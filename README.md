[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)


This is an open source integration for the CoolMaster online cloud service. In opposed to the local CoolMaster integration, this integration will work with devices locked to work only with the cloud (iocControl)

In Order to use this integration, your devices need to be registered with iocControl and you need to have a valid username and password for the service.

When installing the integration all of your controllable units will be added to Home Assistant. Currently the integration only supports HVAC units and doesn't support water heaters.

## How it works

The integration uses CoolAutomation's WebSocket API for real-time updates. When a unit's state changes — including changes made from a wall remote or another app — the new state appears in Home Assistant within a couple of seconds. A bulk HTTP poll runs every five minutes as a drift safety net.

For contributors and maintainers, see [CLAUDE.md](CLAUDE.md) for architecture and release flow.
