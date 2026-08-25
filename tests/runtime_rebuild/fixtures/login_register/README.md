# Durable loop login/register fixture

This deliberately small project is the provider-neutral acceptance fixture for
the long-running loop.  It contains a login/register surface, a stale-edit
target, and a bounded large log so the harness can exercise read, mutation,
command change-set, restart, child, and follow-up paths without credentials.

The fixture is data-only.  It does not claim that a real third-party provider
or a production authentication service has been exercised.
