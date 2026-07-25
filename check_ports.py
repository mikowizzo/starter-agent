import subprocess
import os

# Check what's on port 3100
r = subprocess.run(["docker", "ps", "--filter", "publish=3100", "--format", "{{.Names}} {{.Ports}}"], capture_output=True, text=True)
print("Containers publishing 3100:", r.stdout or "none")
print("Stderr:", r.stderr)

# Check all containers
r2 = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}} {{.Ports}} {{.Status}}"], capture_output=True, text=True)
print("\nAll containers:")
print(r2.stdout or "none")

# Check if starter2 containers exist
r3 = subprocess.run(["docker", "ps", "-a", "--filter", "name=starter2", "--format", "{{.Names}} {{.Status}}"], capture_output=True, text=True)
print("\nStarter2 containers:", r3.stdout or "none")

# Try cleaning up
print("\n--- Cleaning up ---")
subprocess.run(["docker", "rm", "-f", "starter2-frontend-1", "starter2-backend-1"], capture_output=True, text=True)
subprocess.run(["docker", "network", "rm", "starter2_default"], capture_output=True, text=True)

# Check port 3100 again
r4 = subprocess.run(["docker", "ps", "--filter", "publish=3100", "--format", "{{.Names}}"], capture_output=True, text=True)
print("After cleanup, port 3100 used by:", r4.stdout or "free!")
