"""Stub of PHY_model: delays are not needed for hop-count routing."""


def link_delay_ns(kind, length=0, variant=None):
    return 0


def node_forwarding_delay_ns(kind):
    return 0
