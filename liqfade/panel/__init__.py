"""FastAPI backend plus the built React bundle for the web panel.

Runs as its own process on 127.0.0.1:8787 and is reached over Tailscale or an SSH
tunnel. It reads data/state.json rather than the control socket so a slow browser can
never stall the trading loop. Governed by CLAUDE.md sections 1, 4 and 5.
"""
