import os
import sys

def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.gateway")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc

    # Insert "run_gateway" as the first argument to execute_from_command_line
    # so that when user runs `gateway.exe --once`, Django sees `gateway.exe run_gateway --once`
    sys.argv.insert(1, "run_gateway")
    execute_from_command_line(sys.argv)

if __name__ == "__main__":
    main()
