"""Retired memory hook: intentionally inert for clients caching an old registration.

Run scripts/remove-memory-hooks.py --apply to remove the registrations. Background
synchronization is managed by sync_memory.py; this compatibility file never saves,
blocks a turn, or asks an agent to do anything.
"""
