#!/usr/bin/env python3
"""
Python script for reading a BME280 sensor on a raspberry pi and reporting back via MQTT
"""

# pylint: disable=no-member
# pylint: disable=unused-argument

import time
import datetime
import platform
#import math # needed only for detailed sealavel pressure calculation
import os
import signal
import sys

import argparse
import configparser
import daemon
from daemon import pidfile
import paho.mqtt.client as mqtt

try:
    from smbus2 import SMBus
except ImportError:
    from smbus import SMBus

# Pirimoni Python library for the BME680 temperature, pressure and humidity sensor
import bme680

MQTT_INI = "/etc/mqtt.ini"
MQTT_SEC = "bme680"

SEALEVEL_MIN = -999

def receive_signal(signal_number, frame):
    """function to attach to a signal handler, and simply exit
    """

    print('Received signal: ', signal_number)
    sys.exit(0)


def on_connect(client, userdata, flags, return_code):
    """function to mark the connection to a MQTT server
    """

    if return_code != 0:
        print("Connected with result code: ", str(return_code))


def start_daemon(args):
    """function to start daemon in context, if requested
    """

    context = daemon.DaemonContext(
        working_directory='/var/tmp',
        umask=0o002,
        pidfile=pidfile.TimeoutPIDLockFile(args.pid_file),
        )

    context.signal_map = {
        signal.SIGHUP: receive_signal,
        signal.SIGINT: receive_signal,
        signal.SIGQUIT: receive_signal,
        signal.SIGTERM: receive_signal,
    }

    with context:
        start_bme680_sensor(args)

def start_bme680_sensor(args):
    """Main program function, parse arguments, read configuration,
    setup client, listen for messages"""

    i2c_address = bme680.I2C_ADDRESS_GND # 0x76, alt is 0x77

    toffset = 0
    hoffset = 0
    poffset = 0
    elevation = SEALEVEL_MIN
    burn_in_time = 300  # burn_in_time (in seconds) is kept track of.

    if args.daemon:
        file_handle = open(args.log_file, "w")
    else:
        file_handle = sys.stdout

    client = mqtt.Client(args.clientid)

    mqtt_conf = configparser.ConfigParser()
    mqtt_conf.read(args.config)

    topic = mqtt_conf.get(args.section, 'topic')

    if mqtt_conf.has_option(args.section, 'address'):
        i2c_address = int(mqtt_conf.get(args.section, 'address'), 0)

    if mqtt_conf.has_option(args.section, 'toffset'):
        toffset = float(mqtt_conf.get(args.section, 'toffset'))

    if mqtt_conf.has_option(args.section, 'hoffset'):
        hoffset = float(mqtt_conf.get(args.section, 'hoffset'))

    if mqtt_conf.has_option(args.section, 'poffset'):
        poffset = float(mqtt_conf.get(args.section, 'poffset'))

    if mqtt_conf.has_option(args.section, 'elevation'):
        elevation = float(mqtt_conf.get(args.section, 'elevation'))

    if mqtt_conf.has_option(args.section, 'burnin'):
        burn_in_time = float(mqtt_conf.get(args.section, 'burnin'))

    if (mqtt_conf.has_option(args.section, 'username') and
            mqtt_conf.has_option(args.section, 'password')):
        username = mqtt_conf.get(args.section, 'username')
        password = mqtt_conf.get(args.section, 'password')
        client.username_pw_set(username=username, password=password)

    host = mqtt_conf.get(args.section, 'host')
    port = int(mqtt_conf.get(args.section, 'port'))

    client.on_connect = on_connect
    client.connect(host, port, 60)
    client.loop_start()

    topic_temp  = topic + '/' + 'bme680-temperature'
    topic_hum   = topic + '/' + 'bme680-humidity'
    topic_press = topic + '/' + 'bme680-pressure'
    topic_press_S = topic + '/' + 'bme680-sealevel-pressure'
    topic_aqi   = topic + '/' + 'bme680-air-quality'
    
    # Initialise the BME280
    bus = SMBus(1)

    sensor = bme680.BME680(i2c_addr=i2c_address, i2c_dev=bus)

    if args.verbose:
        curr_datetime = datetime.datetime.now()
        str_datetime = curr_datetime.strftime("%Y-%m-%d %H:%M:%S")
        print("{0}: pid: {1:d} bme680 sensor started on 0x{2:x}, toffset: {3:0.1f} F, hoffset: {4:0.1f} %, poffset: {5:0.2f} hPa".
              format(str_datetime, os.getpid(), i2c_address, toffset, hoffset, poffset), file=file_handle)
        file_handle.flush()

    # These oversampling settings can be tweaked to 
    # change the balance between accuracy and noise in
    # the data.

    sensor.set_humidity_oversample(bme680.OS_2X)
    sensor.set_pressure_oversample(bme680.OS_4X)
    sensor.set_temperature_oversample(bme680.OS_8X)
    sensor.set_filter(bme680.FILTER_SIZE_3)
    sensor.set_gas_status(bme680.ENABLE_GAS_MEAS)

    sensor.set_gas_heater_temperature(320)
    sensor.set_gas_heater_duration(150)
    sensor.select_gas_heater_profile(0)

     #start_time and curr_time ensure that the burn_in_time (in seconds) is kept track of.

    start_time = time.time()
    curr_time = time.time()

    burn_in_data = []

    # Collect gas resistance burn-in values, then use the average
    # of the last 50 values to set the upper limit for calculating
    # gas_baseline.
    if args.verbose:
        print("Collecting gas resistance burn-in data for 5 mins\n", file=file_handle) # FIXME: give the real time
    while curr_time - start_time < burn_in_time:
        curr_time = time.time()
        if sensor.get_sensor_data() and sensor.data.heat_stable:
            gas = sensor.data.gas_resistance
            burn_in_data.append(gas)
            my_time = int(round(curr_time))
            if args.verbose:
                if (my_time % 60 == 0): 
                    print("Gas: {0} Ohms".format(gas), file=file_handle) # only if verbose?
            time.sleep(1)

    gas_baseline = sum(burn_in_data[-50:]) / 50.0

    # Set the humidity baseline to 40%, an optimal indoor humidity.
    hum_baseline = 40.0

    # This sets the balance between humidity and gas reading in the 
    # calculation of air_quality_score (25:75, humidity:gas)
    hum_weighting = 0.25

    curr_datetime = datetime.datetime.now()
    str_datetime = curr_datetime.strftime("%Y-%m-%d %H:%M:%S")
    print("{0}: pid: {1:d} Gas baseline: {0} Ohms, humidity baseline: {1:.2f} %RH\n".
          format(str_datetime, os.getpid(), gas_baseline, hum_baseline), file=file_handle)


    while True:
        if sensor.get_sensor_data() and sensor.data.heat_stable:
            
            curr_time = time.time()
            gas = sensor.data.gas_resistance
            gas_offset = gas_baseline - gas

            hum = sensor.data.humidity + hoffset
            hum_offset = hum - hum_baseline

            temp_C = sensor.data.temperature
            temp_F = 9.0/5.0 * temp_C + 32 + toffset
            temp_K = temp_C + 273.15

            press_A = sensor.data.pressure + poffset

            # https://www.sandhurstweather.org.uk/barometric.pdf
            if elevation > SEALEVEL_MIN:
                # option one: Sea Level Pressure = Station Pressure / e ** -elevation / (temperature x 29.263)
                #press_S = press_A / math.exp( - elevation / (temp_K * 29.263))
                # option two: Sea Level Pressure = Station Pressure + (elevation/9.2)
                press_S = press_A + (elevation/9.2)
            else:
                press_S = press_A

            # Calculate hum_score as the distance from the hum_baseline.
            if hum_offset > 0:
                hum_score = (100 - hum_baseline - hum_offset) / (100 - hum_baseline) * (hum_weighting * 100)

            else:
                hum_score = (hum_baseline + hum_offset) / hum_baseline * (hum_weighting * 100)

            # Calculate gas_score as the distance from the gas_baseline.
            if gas_offset > 0:
                gas_score = (gas / gas_baseline) * (100 - (hum_weighting * 100))

            else:
                gas_score = 100 - (hum_weighting * 100)

            # Calculate air_quality_score. 
            air_quality_score = hum_score + gas_score
           
            my_time = int(round(curr_time))


            if (my_time % 60 == 0): 

                curr_datetime = datetime.datetime.now()
                str_datetime = curr_datetime.strftime("%Y-%m-%d %H:%M:%S")
                
                if args.verbose:
                    print("{0}: Gas: {1:.2f} Ohms, temperature: {2:.2f} F, humidity: {3:.2f} %RH, air quality: {4:.2f}".
                          format(str_datetime, gas, temp, hum, air_quality_score), file=file_handle)
#                    print("{0}: temperature: {1:.1f} F, humidity: {2:.1f} %, pressure: {3:.2f} hPa, sealevel: {4:.2f} hPa".
#                          format(str_datetime, temp_F, hum, press_A, press_S), file=file_handle)
                    file_handle.flush()


                humidity = str(round(hum, 2))
                temperature = str(round(temp_F, 2))
                pressure = str(round(press_A, 2))
                pressure_sealevel = str(round(press_S, 2))
                air_qual = str(round(air_quality_score, 2))
            
                client.publish(topic_hum, humidity)
                client.publish(topic_temp, temperature)
                client.publish(topic_press, pressure)
                
                if elevation > SEALEVEL_MIN:
                    client.publish(topic_press_S, pressure_sealevel)

                client.publish(topic_aqi, air_qual)

            time.sleep(1)

if __name__ == '__main__':

    #myhost = socket.gethostname().split('.', 1)[0]
    myhost = platform.node()
    mypid  = os.getpid()
    clientId = myhost + '-' + str(mypid)

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-c', '--config', default=MQTT_INI, help="configuration file")
    parser.add_argument('-d', '--daemon', action='store_true', help="run as daemon")
    parser.add_argument('-p', '--pid-file', default='/var/run/bme680_mqtt.pid')
    parser.add_argument('-l', '--log-file', default='/var/log/bme680_mqtt.log')
    parser.add_argument('-i', '--clientid', default=clientId, help="clientId for MQTT connection")
    parser.add_argument('-s', '--section', default=MQTT_SEC, help="configuration file section")
    parser.add_argument('-v', '--verbose', action='store_true', help="verbose messages")

    args = parser.parse_args()

    if args.daemon:
        start_daemon(args)
    else:
        start_bme680_sensor(args)
