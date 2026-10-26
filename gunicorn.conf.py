import os

# Workers: 2 × CPU + 1 (Render Starter tiene 1 CPU)
workers = 3
worker_class = "sync"
timeout = 60
keepalive = 5

# Bind
bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"

# Logs
accesslog = "-"
errorlog = "-"
loglevel = "info"
