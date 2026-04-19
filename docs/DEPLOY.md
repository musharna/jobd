# Deployment

## Broker (broker-host)

    cd ~/jobd
    docker compose up -d
    docker compose logs -f jobd

## Worker (desktop-worker) — interim nohup mode

    ssh homelab "cat ~/jobd/worker/job_worker.py" | ssh desktop-worker "cat > ~/jobd-worker/job_worker.py"
    ssh desktop-worker "cd ~/jobd-worker && JOBD_URL=http://10.0.0.10:8765 JOBD_WORKER_HOST=desktop nohup .venv/bin/python job_worker.py > logs/worker.log 2>&1 & disown"

Survives ssh disconnect but not WSL/host reboot. Systemd deploy below lands with
Phase-0 Task 2 (needs loginctl enable-linger, sudo).

## Worker (desktop-worker) — target systemd deploy (requires Task 2)

    # One-time (sudo required):
    sudo loginctl enable-linger mjarnold

    # Then:
    mkdir -p ~/.config/systemd/user
    ssh homelab "cat ~/jobd/worker/job-worker.service" > ~/.config/systemd/user/job-worker.service
    systemctl --user daemon-reload
    systemctl --user enable --now job-worker.service

## Worker status + logs

    ssh desktop-worker "pgrep -af job_worker.py"
    ssh desktop-worker "tail -50 ~/jobd-worker/logs/worker.log"

## Redeploying worker after code changes

    ssh desktop-worker "pkill -f job_worker.py || true"
    ssh homelab "cat ~/jobd/worker/job_worker.py" | ssh desktop-worker "cat > ~/jobd-worker/job_worker.py"
    ssh desktop-worker "cd ~/jobd-worker && JOBD_URL=http://10.0.0.10:8765 JOBD_WORKER_HOST=desktop nohup .venv/bin/python job_worker.py > logs/worker.log 2>&1 & disown"

## Adding a new worker host (Phase 2+)

1. `scp -r ~/jobd <host>:~/jobd-src` (or git clone the repo)
2. `ssh <host> "bash ~/jobd-src/scripts/install-worker.sh --broker http://10.0.0.10:8765 --host <name>"`
3. Verify heartbeat: `curl http://10.0.0.10:8765/health && sleep 15 && job list`
4. (Optional) enable auto-start: `sudo loginctl enable-linger $USER && systemctl --user enable --now job-worker.service`
