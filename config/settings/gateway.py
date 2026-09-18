"""
Settings for the field gateway — the folder watcher that runs at a site.

The gateway is an HTTP client with a watched folder. It reads instruments'
export files off a disk and POSTs them to the platform; it never reads or
writes the platform's database, and it must never be able to. This module
takes the platform's ordinary settings and removes the two things that would
let it: the database, and the URL routing.

**``DATABASES`` is empty, deliberately.** Not "pointed at localhost" — empty.
A site laptop is a shared machine in a site office beside the instrument, and
the one thing it must not hold is a credential to the statutory registry. With
no database configured there is nothing for a mistake in this process to
reach, whether or not a stray ``.env`` ever lands beside it. This is the
load-bearing line: nothing else in this file is a security boundary. Nothing
in this codebase touches the ORM under these settings — the gateway talks to
the platform over HTTPS like any other client.

**There is no URLconf.** Set to ``None`` rather than left alone, because
Django's URL checks honour exactly this value: with a URLconf configured,
``manage.py check`` imports ``config.urls``, which imports every app's routes
for a process that routes nothing. ``None`` makes those checks no-ops, and
states plainly that this process serves nothing.

Everything else is inherited unchanged, including ``INSTALLED_APPS``,
``MIDDLEWARE`` and ``TEMPLATES``. Two attempts to narrow those are worth
recording, because both looked like improvements and neither was.

Trimming ``INSTALLED_APPS`` to the gateway's foreign-key closure saves about
three tenths of a second of startup, and costs a ``manage.py check`` that
fails with six unresolvable foreign keys — a confusing error from a command
that has nothing to do with the gateway, on the machine least able to diagnose
it. Blanking ``MIDDLEWARE`` and ``TEMPLATES`` does nothing at all except fail
four admin checks, because ``django.contrib.admin`` is still an installed app.
Neither bought anything that ``DATABASES = {}`` does not already provide, and
this file is better for being two lines than for being clever.

Run the gateway as::

    DJANGO_SETTINGS_MODULE=config.settings.gateway python manage.py run_gateway --config gateway.json

or, on Windows, through the ``run_gateway.bat`` wrapper beside the repository
root, which sets this module and invokes the virtualenv's Python.
"""
from .base import *  # noqa: F401,F403 — the platform's settings, then narrowed

#: No database. See the module docstring — this is the load-bearing line, and
#: the only one in this file that is a security boundary.
DATABASES = {}

#: No routing. Nothing is served under these settings.
ROOT_URLCONF = None
