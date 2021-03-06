from datetime import time
from decimal import Decimal, ROUND_UP
import pytz

from django.utils import timezone
from django.db.models import Avg, Min, Max, Count

from dsmr_consumption.models.consumption import ElectricityConsumption, GasConsumption
from dsmr_consumption.models.settings import ConsumptionSettings
from dsmr_consumption.models.energysupplier import EnergySupplierPrice
from dsmr_datalogger.models.reading import DsmrReading
from dsmr_weather.models.reading import TemperatureReading
from dsmr_stats.models.note import Note


def compact_all():
    """ Compacts all unprocessed readings, capped by a max to prevent hanging backend. """
    unprocessed_readings = DsmrReading.objects.unprocessed()[0:1000]

    for current_reading in unprocessed_readings:
        compact(dsmr_reading=current_reading)


def compact(dsmr_reading):
    """
    Compacts/converts DSMR readings to consumption data. Optionally groups electricity by minute.
    """
    grouping_type = ConsumptionSettings.get_solo().compactor_grouping_type

    # Electricity should be unique, because it's the reading with the lowest interval anyway.
    if grouping_type == ConsumptionSettings.COMPACTOR_GROUPING_BY_READING:
        ElectricityConsumption.objects.get_or_create(
            read_at=dsmr_reading.timestamp,
            delivered_1=dsmr_reading.electricity_delivered_1,
            returned_1=dsmr_reading.electricity_returned_1,
            delivered_2=dsmr_reading.electricity_delivered_2,
            returned_2=dsmr_reading.electricity_returned_2,
            currently_delivered=dsmr_reading.electricity_currently_delivered,
            currently_returned=dsmr_reading.electricity_currently_returned,
            phase_currently_delivered_l1=dsmr_reading.phase_currently_delivered_l1,
            phase_currently_delivered_l2=dsmr_reading.phase_currently_delivered_l2,
            phase_currently_delivered_l3=dsmr_reading.phase_currently_delivered_l3,
        )
    # Grouping by minute requires some distinction and history checking.
    else:
        minute_start = timezone.datetime.combine(
            dsmr_reading.timestamp.date(),
            time(hour=dsmr_reading.timestamp.hour, minute=dsmr_reading.timestamp.minute),
        ).replace(tzinfo=pytz.UTC)
        minute_end = minute_start + timezone.timedelta(minutes=1)

        # Postpone when current minute hasn't passed yet.
        if timezone.now() <= minute_end:
            return

        # We might have six readings per minute, so there is a chance we already parsed it.
        if not ElectricityConsumption.objects.filter(read_at=minute_end).exists():
            grouped_reading = DsmrReading.objects.filter(
                timestamp__gte=minute_start, timestamp__lt=minute_end
            ).aggregate(
                avg_delivered=Avg('electricity_currently_delivered'),
                avg_returned=Avg('electricity_currently_returned'),
                max_delivered_1=Max('electricity_delivered_1'),
                max_delivered_2=Max('electricity_delivered_2'),
                max_returned_1=Max('electricity_returned_1'),
                max_returned_2=Max('electricity_returned_2'),
                avg_phase_delivered_l1=Avg('phase_currently_delivered_l1'),
                avg_phase_delivered_l2=Avg('phase_currently_delivered_l2'),
                avg_phase_delivered_l3=Avg('phase_currently_delivered_l3'),
            )

            # This instance is the average/max and combined result.
            ElectricityConsumption.objects.create(
                read_at=minute_end,
                delivered_1=grouped_reading['max_delivered_1'],
                returned_1=grouped_reading['max_returned_1'],
                delivered_2=grouped_reading['max_delivered_2'],
                returned_2=grouped_reading['max_returned_2'],
                currently_delivered=grouped_reading['avg_delivered'],
                currently_returned=grouped_reading['avg_returned'],
                phase_currently_delivered_l1=grouped_reading['avg_phase_delivered_l1'],
                phase_currently_delivered_l2=grouped_reading['avg_phase_delivered_l2'],
                phase_currently_delivered_l3=grouped_reading['avg_phase_delivered_l3'],
            )

    # Gas is optional.
    if dsmr_reading.extra_device_timestamp and dsmr_reading.extra_device_delivered:
        # Gas however is only read (or updated) once every hour, so we should check for any duplicates
        # as they will exist at some point.
        passed_hour_start = dsmr_reading.extra_device_timestamp - timezone.timedelta(hours=1)

        if not GasConsumption.objects.filter(read_at=passed_hour_start).exists():
            # DSMR does not expose current gas rate, so we have to calculate
            # it ourselves, relative to the previous gas consumption, if any.
            try:
                previous_gas_consumption = GasConsumption.objects.get(
                    # Compare to reading before, if any.
                    read_at=passed_hour_start - timezone.timedelta(hours=1)
                )
            except GasConsumption.DoesNotExist:
                gas_diff = 0
            else:
                gas_diff = dsmr_reading.extra_device_delivered - previous_gas_consumption.delivered

            GasConsumption.objects.create(
                # Gas consumption is aligned to start of the hour.
                read_at=passed_hour_start,
                delivered=dsmr_reading.extra_device_delivered,
                currently_delivered=gas_diff
            )

    dsmr_reading.processed = True
    dsmr_reading.save(update_fields=['processed'])

    # For backend logging in Supervisor.
    print(' - Processed reading: {}.'.format(timezone.localtime(dsmr_reading.timestamp)))


def consumption_by_range(start, end):
    """ Calculates the consumption of a range specified. """
    electricity_readings = ElectricityConsumption.objects.filter(
        read_at__gte=start, read_at__lt=end,
    ).order_by('read_at')

    gas_readings = GasConsumption.objects.filter(
        read_at__gte=start, read_at__lt=end,
    ).order_by('read_at')

    return electricity_readings, gas_readings


def day_consumption(day):
    """ Calculates the consumption of an entire day. """
    consumption = {
        'day': day
    }
    day_start = timezone.make_aware(timezone.datetime(year=day.year, month=day.month, day=day.day))
    day_end = day_start + timezone.timedelta(days=1)

    try:
        # This WILL fail when we either have no prices at all or conflicting ranges.
        consumption['daily_energy_price'] = EnergySupplierPrice.objects.by_date(target_date=day)
    except (EnergySupplierPrice.DoesNotExist, EnergySupplierPrice.MultipleObjectsReturned):
        # Default to zero prices.
        consumption['daily_energy_price'] = EnergySupplierPrice()

    electricity_readings, gas_readings = consumption_by_range(start=day_start, end=day_end)

    if not electricity_readings.exists():
        raise LookupError("No electricity readings found for: {}".format(day))

    electricity_reading_count = electricity_readings.count()

    first_reading = electricity_readings[0]
    last_reading = electricity_readings[electricity_reading_count - 1]
    consumption['electricity1'] = last_reading.delivered_1 - first_reading.delivered_1
    consumption['electricity2'] = last_reading.delivered_2 - first_reading.delivered_2
    consumption['electricity1_start'] = first_reading.delivered_1
    consumption['electricity1_end'] = last_reading.delivered_1
    consumption['electricity2_start'] = first_reading.delivered_2
    consumption['electricity2_end'] = last_reading.delivered_2
    consumption['electricity1_returned'] = last_reading.returned_1 - first_reading.returned_1
    consumption['electricity2_returned'] = last_reading.returned_2 - first_reading.returned_2
    consumption['electricity1_returned_start'] = first_reading.returned_1
    consumption['electricity1_returned_end'] = last_reading.returned_1
    consumption['electricity2_returned_start'] = first_reading.returned_2
    consumption['electricity2_returned_end'] = last_reading.returned_2
    consumption['electricity1_unit_price'] = consumption['daily_energy_price'].electricity_1_price
    consumption['electricity2_unit_price'] = consumption['daily_energy_price'].electricity_2_price
    consumption['electricity1_cost'] = round_decimal(
        consumption['electricity1'] * consumption['electricity1_unit_price']
    )
    consumption['electricity2_cost'] = round_decimal(
        consumption['electricity2'] * consumption['electricity2_unit_price']
    )
    consumption['electricity_merged'] = consumption['electricity1'] + consumption['electricity2']
    consumption['electricity_returned_merged'] = \
        consumption['electricity1_returned'] + consumption['electricity2_returned']
    consumption['electricity_cost_merged'] = consumption['electricity1_cost'] + consumption['electricity2_cost']
    consumption['total_cost'] = round_decimal(
        consumption['electricity1_cost'] + consumption['electricity2_cost']
    )

    # Gas readings are optional, as not all meters support this.
    if gas_readings.exists():
        gas_reading_count = gas_readings.count()
        first_reading = gas_readings[0]
        last_reading = gas_readings[gas_reading_count - 1]
        consumption['gas'] = last_reading.delivered - first_reading.delivered
        consumption['gas_start'] = first_reading.delivered
        consumption['gas_end'] = last_reading.delivered
        consumption['gas_unit_price'] = consumption['daily_energy_price'].gas_price
        consumption['gas_cost'] = round_decimal(
            consumption['gas'] * consumption['gas_unit_price']
        )
        consumption['total_cost'] += consumption['gas_cost']

    consumption['notes'] = Note.objects.filter(day=day).values_list('description', flat=True)

    # Remperature readings are not mandatory as well.
    temperature_readings = TemperatureReading.objects.filter(
        read_at__gte=day_start, read_at__lt=day_end,
    ).order_by('read_at')
    consumption['lowest_temperature'] = temperature_readings.aggregate(
        avg_temperature=Min('degrees_celcius'),
    )['avg_temperature'] or 0
    consumption['highest_temperature'] = temperature_readings.aggregate(
        avg_temperature=Max('degrees_celcius'),
    )['avg_temperature'] or 0
    consumption['average_temperature'] = temperature_readings.aggregate(
        avg_temperature=Avg('degrees_celcius'),
    )['avg_temperature'] or 0

    return consumption


def round_decimal(decimal_price):
    """ Round the price to two decimals. """
    if not isinstance(decimal_price, Decimal):
        decimal_price = Decimal(str(decimal_price))

    return decimal_price.quantize(Decimal('.01'), rounding=ROUND_UP)


def calculate_slumber_consumption_watt():
    """ Groups all electricity readings to find the most constant consumption. """
    most_common = ElectricityConsumption.objects.filter(
        currently_delivered__gt=0
    ).values('currently_delivered').annotate(
        currently_delivered_count=Count('currently_delivered')
    ).order_by('-currently_delivered_count')[:5]

    if not most_common:
        return

    # We calculate the average among the most common consumption read.
    count = 0
    usage = 0

    for item in most_common:
        count += item['currently_delivered_count']
        usage += item['currently_delivered_count'] * item['currently_delivered']

    return round(usage / count * 1000)


def calculate_min_max_consumption_watt():
    """ Returns the lowest and highest Wattage consumed. """
    min_max = ElectricityConsumption.objects.filter(
        currently_delivered__gt=0
    ).aggregate(
        min_watt=Min('currently_delivered'),
        max_watt=Max('currently_delivered')
    )

    for x in min_max.keys():
        if min_max[x]:
            min_max[x] = int(min_max[x] * 1000)

    return min_max
