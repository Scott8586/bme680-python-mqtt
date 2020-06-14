#
# Makefile for installing bme680_mqtt_daemon.py
INSTDIR = /usr/local/bin
SVCDIR  = /etc/systemd/system

#install: $(INSTDIR)/bme680_mqtt_daemon.py $(SVCDIR)/bme680-mqtt.service

install: install.daemon install.service
install.daemon: $(INSTDIR)/bme680_mqtt_daemon.py
install.service: $(SVCDIR)/bme680-mqtt.service

$(INSTDIR)/bme680_mqtt_daemon.py: bme680_mqtt_daemon.py
	cp $? $(INSTDIR)

$(SVCDIR)/bme680-mqtt.service: bme680-mqtt.service
	cp $? $(SVCDIR)
	systemctl daemon-reload
#	systemctl enable $?
#	systemctl start $?

clobber: clobber.daemon clobber.service

clobber.daemon:
	rm -f $(INSTDIR)/bme680_mqtt_daemon.py

clobber.service: bme680-mqtt.service
	systemctl stop $?
	systemctl disable $?
	rm -f $(SVCDIR)/bme680-mqtt.service
	systemctl daemon-reload
