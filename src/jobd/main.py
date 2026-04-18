"""Entry point: python -m jobd or `jobd` after install."""
import logging
import os

import uvicorn

from jobd.app import build_app


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    config_dir = os.environ.get("JOBD_CONFIG_DIR", "/app/config")
    db_url = os.environ.get("JOBD_DB_URL", "sqlite:////app/data/jobd.db")
    app = build_app(
        db_url=db_url,
        projects_path=f"{config_dir}/projects.yaml",
        profiles_path=f"{config_dir}/profiles.yaml",
        classifier_path=f"{config_dir}/classifier.yaml",
    )
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("JOBD_PORT", "8765")))


if __name__ == "__main__":
    run()
