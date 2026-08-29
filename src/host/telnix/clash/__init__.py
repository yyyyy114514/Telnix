"""Clash/Mihomo integration module.

External mode: Telnix does not start the Mihomo process; it only connects to a
user-running Clash/Mihomo via the external-controller API.
The Telnix proxy forwards traffic to Mihomo's mixed-port, enabling capture + node proxy.
"""
