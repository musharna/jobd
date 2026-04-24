# Deployment

## Broker (gt76)

    cd ~/jobd
    docker compose up -d
    docker compose logs -f jobd

## Worker (desktop-wsl) — interim nohup mode

    ssh homelab "cat ~/jobd/worker/job_worker.py" | ssh desktop-wsl "cat > ~/jobd-worker/job_worker.py"
    ssh desktop-wsl "cd ~/jobd-worker && JOBD_URL=http://100.113.204.41:8765 JOBD_WORKER_HOST=desktop nohup .venv/bin/python job_worker.py > logs/worker.log 2>&1 & disown"

Survives ssh disconnect but not WSL/host reboot. Systemd deploy below lands with
Phase-0 Task 2 (needs loginctl enable-linger, sudo).

## Worker (desktop-wsl) — target systemd deploy (requires Task 2)

One-time install (needs sudo on desktop-wsl; cannot be done via SSH key login
without sudoers tweaks — log in interactively):

    ssh desktop-wsl
    sudo loginctl enable-linger mjarnold
    mkdir -p ~/.config/systemd/user
    scp homelab:~/jobd/worker/job-worker.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now job-worker.service

Verify:

    systemctl --user status job-worker.service
    journalctl --user -u job-worker.service -f

Redeploy after code changes (systemd mode):

    ssh homelab "cat ~/jobd/worker/job_worker.py" | ssh desktop-wsl "cat > ~/jobd-worker/job_worker.py"
    ssh desktop-wsl "systemctl --user restart job-worker.service"

## Worker status + logs

    ssh desktop-wsl "pgrep -af job_worker.py"
    ssh desktop-wsl "tail -50 ~/jobd-worker/logs/worker.log"

## Redeploying worker after code changes

    ssh desktop-wsl "pkill -f job_worker.py || true"
    ssh homelab "cat ~/jobd/worker/job_worker.py" | ssh desktop-wsl "cat > ~/jobd-worker/job_worker.py"
    ssh desktop-wsl "cd ~/jobd-worker && JOBD_URL=http://100.113.204.41:8765 JOBD_WORKER_HOST=desktop nohup .venv/bin/python job_worker.py > logs/worker.log 2>&1 & disown"

## Adding a new worker host (Phase 2+)

1. `scp -r ~/jobd <host>:~/jobd-src` (or git clone the repo)
2. `ssh <host> "bash ~/jobd-src/scripts/install-worker.sh --broker http://100.113.204.41:8765 --host <name>"`
3. Verify heartbeat: `curl http://100.113.204.41:8765/health && sleep 15 && job list`
4. (Optional) enable auto-start: `sudo loginctl enable-linger $USER && systemctl --user enable --now job-worker.service`
