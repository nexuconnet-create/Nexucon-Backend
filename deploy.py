import paramiko
import sys
import time

def deploy():
    host = '187.7.20.123'
    username = 'root'
    password = "3FPw#'1Uw2AA@y@O"
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        print(f"Connecting to {host}...")
        client.connect(hostname=host, username=username, password=password, timeout=10)
        print("Connected successfully.")
        
        # The user was working in /opt/nexucon-backend
        # Also there's a makemigrations warning from the user's log:
        # "Your models in app(s): 'evidence' have changes that are not yet reflected in a migration"
        # We will make sure to makemigrations and migrate if needed, though they just ran it.
        
        commands = [
            "cd /opt/nexucon-backend",
            "git pull origin main",
            "docker compose build web celery gateway",
            "docker compose up -d",
            "docker compose exec -T web python manage.py makemigrations",
            "docker compose exec -T web python manage.py migrate",
            "docker compose ps"
        ]
        
        cmd_str = " && ".join(commands)
        print(f"Running command: {cmd_str}")
        
        stdin, stdout, stderr = client.exec_command(cmd_str, get_pty=True)
        
        # Read the output line by line
        for line in iter(stdout.readline, ""):
            sys.stdout.buffer.write(line.encode('utf-8', errors='replace'))
            sys.stdout.flush()
            
        exit_status = stdout.channel.recv_exit_status()
        print(f"\\nCommand finished with exit status: {exit_status}")
        
    except Exception as e:
        print(f"Error during deployment: {e}")
    finally:
        client.close()

if __name__ == "__main__":
    deploy()
