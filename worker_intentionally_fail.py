import sys

print("Controlled failure test: this worker is designed to fail.", file=sys.stderr)
sys.exit(1)
