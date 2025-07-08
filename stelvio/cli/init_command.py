import logging
import textwrap
import time
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.text import Text

logger = logging.getLogger(__name__)

TEMPLATE_CONTENT = """\
from stelvio.app import StelvioApp
from stelvio.config import StelvioAppConfig, AwsConfig

app = StelvioApp("{project_name}")

@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(
            # region="us-east-1",        # Uncomment to override AWS CLI/env var region
            # profile="your-profile",    # Uncomment to use specific AWS profile
        ),
    )

@app.run
def run() -> None:
    # Create your infra here
    pass
"""


def stelvio_art(console: Console) -> None:
    art_lines_raw = [
        " ____  _       _       _       ",
        "/ ___|| |_ ___| |_   _(_) ___  ",
        "\\___ \\| __/ _ \\ \\ \\ / / |/ _ \\",
        " ___) | ||  __/ |\\ V /| | (_) |",
        "|____/ \\__\\___|_| \\_/ |_|\\___/ ",
    ]

    max_len = max([len(line) for line in art_lines_raw])
    normalized_art_lines = [line.ljust(max_len) for line in art_lines_raw]

    reveal_color = "bold turquoise2"
    cursor_char = "_"
    reveal_delay = 0.03

    with Live(console=console, refresh_per_second=30, transient=False) as live:
        for column in range(max_len + 1):
            frame_text = Text()
            for full_line in normalized_art_lines:
                revealed_part = full_line[:column]
                remaining_part_len = max_len - column

                frame_text.append(revealed_part, style=reveal_color)

                if column < max_len:
                    frame_text.append("_", style="dim grey70")
                    frame_text.append(" " * (remaining_part_len - (1 if cursor_char else 0)))
                else:
                    frame_text.append(" " * remaining_part_len)
                frame_text.append("\n")

            live.update(frame_text)
            if column == max_len:
                break
            time.sleep(reveal_delay)

        final_art_text = Text()
        for full_line in normalized_art_lines:
            final_art_text.append(full_line + "\n", style=reveal_color)
        live.update(final_art_text)


def get_stlv_app_path() -> tuple[Path, bool]:
    cwd = Path.cwd()
    logger.info("CWD %s", cwd)
    logger.info("Dir name: %s", cwd.name)
    stlv_app = "stlv_app.py"
    stlv_app_path = cwd / stlv_app
    return stlv_app_path, stlv_app_path.exists() and stlv_app_path.is_file()


def create_stlv_app_file(stlv_app_path: Path) -> None:
    file_content = textwrap.dedent(TEMPLATE_CONTENT).format(project_name=Path.cwd().name)
    with stlv_app_path.open("w", encoding="utf-8") as f:
        f.write(file_content)
