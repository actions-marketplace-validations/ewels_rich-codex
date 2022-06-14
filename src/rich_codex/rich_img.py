import difflib
import fcntl
import logging
import os
import pty
import re
import signal
import struct
import subprocess
from pathlib import Path
import termios
from shutil import copyfile
from tempfile import gettempdir, mkstemp

import rich.terminal_theme
from Levenshtein import ratio
from rich.ansi import AnsiDecoder
from rich.console import Console
from rich.prompt import Confirm
from rich.syntax import Syntax

log = logging.getLogger("rich-codex")

# Attributes of RichImg which are important for equality
RICH_IMG_ATTRS = [
    "terminal_width",
    "terminal_theme",
    "title",
    "cmd",
    "snippet",
    "snippet_syntax",
    "img_paths",
]

# Base list of commands to ignore
IGNORE_COMMANDS = ["rm", "cp", "mv", "sudo"]

# Base list of diff regexes to ignore, split up by filetype suffix
IGNORE_REGEXES = {".pdf": [r"/CreationDate"]}


class RichImg:
    """Image generation for rich-codex.

    Objects from this class are typically used once per screenshot.
    """

    def __init__(
        self,
        snippet_syntax=None,
        min_pct_diff=0,
        skip_change_regex=None,
        terminal_width=None,
        terminal_theme=None,
        use_pty=False,
        console=None,
    ):
        """Initialise the RichImg object with core console options."""
        self.snippet_syntax = snippet_syntax
        self.min_pct_diff = min_pct_diff
        self.skip_change_regex = skip_change_regex
        self.terminal_width = terminal_width
        self.terminal_theme = terminal_theme
        self.use_pty = use_pty
        self.title = ""
        self.console = Console() if console is None else console
        self.capture_console = Console(
            file=open(os.devnull, "w"),
            force_terminal=True,
            color_system="truecolor",
            highlight=False,
            record=True,
            width=int(terminal_width) if terminal_width else None,
        )
        self.cwd = Path.cwd()
        self.cmd = None
        self.snippet = None
        self.img_paths = []
        self.num_img_saved = 0
        self.num_img_skipped = 0
        self.no_confirm = False
        self.aborted = False

    def __eq__(self, other):
        """Compare RichImg objects for equality."""
        if not isinstance(other, RichImg):
            # don't attempt to compare against unrelated types
            return NotImplemented
        return all(getattr(self, attr) == getattr(other, attr) for attr in RICH_IMG_ATTRS)

    def __hash__(self):
        """Hash stable identifier of RichImg object based on important attributes."""
        attrs = str([getattr(self, attr) for attr in RICH_IMG_ATTRS])
        return hash(attrs)

    def _hash_no_fn(self):
        """Hash stable identifier of RichImg object based without output filenames."""
        attrs = str([getattr(self, attr) for attr in RICH_IMG_ATTRS if attr != "img_paths"])
        return hash(attrs)

    def confirm_command(self):
        """Prompt user to confirm running command."""
        if self.cmd is None or self.no_confirm:
            return True
        return Confirm.ask(f"Command: [white on black] {self.cmd} [/] Run?", console=self.console)

    def run_command(self):
        """Capture output from a supplied command and save to an image."""
        if self.cmd is None:
            log.debug("Tried to generate image with no command")
            return

        self.cmd = self.cmd.strip()

        for ignore in IGNORE_COMMANDS:
            if any(cmd_part.strip().startswith(ignore) for cmd_part in self.cmd.split("&;")):
                log.warning(f"Ignoring command because it contained '{ignore}': [white on black] {self.cmd} [/]")
                self.aborted = True
                return False

        if self.title == "":
            self.title = self.cmd

        # Run the command with a fake tty to try to get colours
        if self.use_pty:
            log.debug(f"Running command '{self.cmd}' with pty")

            read_end, write_end = pty.openpty()

            # Resize routine for pty
            # First, get our own current terminal size
            # (struct is documented here: https://www.delorie.com/djgpp/doc/libc/libc_495.html)
            size = fcntl.ioctl(0, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))

            # Rewrite size with selected terminal width
            if self.terminal_width is not None:
                data = struct.unpack("HHHH", size)
                size = struct.pack("HHHH", data[0], self.terminal_width, data[2], data[3])

            # Issue command to pty to resize
            fcntl.ioctl(read_end, termios.TIOCSWINSZ, size)
            signal.signal(signal.SIGWINCH, lambda s, f: fcntl.ioctl(read_end, termios.TIOCSWINSZ, size))

            # Run subprocess in pty
            process = subprocess.Popen(
                self.cmd,
                shell=True,
                close_fds=True,
                preexec_fn=os.setsid,
                stdout=write_end,
                stderr=write_end,
            )
            os.close(write_end)

            output_arr = []

            # This loop will keep going until no more data is incoming (child process closed their pipe)
            while True:
                try:
                    data = os.read(read_end, 1024)
                except OSError:
                    data = b""

                if data:
                    output_arr.append(data)
                else:
                    break

            os.close(read_end)
            output = b"".join(output_arr).decode("utf-8")

        # Run the command without messing with ttys
        else:
            log.debug(f"Running command '{self.cmd}' with subprocess")
            process = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,  # Needed for pipes
            )
            output = process.stdout.read().decode("utf-8")

        # Decode and print the output (captured)
        decoder = AnsiDecoder()
        for line in decoder.decode(output):
            self.capture_console.print(line)

    def format_snippet(self):
        """Take a text snippet and format it using rich."""
        if self.snippet is None:
            log.debug("Tried to format snippet with no snippet")
            return

        # JSON is a special case, use rich function
        try:
            if self.snippet_syntax == "json" or self.snippet_syntax is None:
                self.capture_console.print_json(json=self.snippet)
                log.debug("Formatting snippet as JSON")
                return
            else:
                raise

        # All other languages, use rich Syntax highlighter (no reformatting whitespace)
        except Exception:
            log.debug(f"Formatting snippet as {self.snippet_syntax}")
            syntax = Syntax(self.snippet, self.snippet_syntax)
            self.capture_console.print(syntax)

    def get_output(self):
        """Either run command or format snippet, depending on what is set."""
        if self.cmd is not None:
            self.run_command()
        elif self.snippet is not None:
            self.format_snippet()
        else:
            log.warning("Tried to get output with no command or snippet")

    def _enough_image_difference(self, new_fn, old_fn):
        new_file = Path(new_fn)
        old_file = Path(old_fn)
        create_file = True
        log_msg = ""

        # Target file doesn't exist yet, nothing to compare against
        if not old_file.exists():
            log_msg = "new image"

        else:
            # Percentage change in file
            # This method works even with entirely binary files, no decoding required
            pct_change = (1 - ratio(new_file.read_bytes(), old_file.read_bytes())) * 100.0
            if pct_change <= float(self.min_pct_diff):
                create_file = False
            log_msg = f"{pct_change:.2f}% change"

            # No point in looking for a diff if the files are identical
            if pct_change > 0:

                # Regex on file diff to skip
                skip_regexes = list(r for r in IGNORE_REGEXES.get(new_file.suffix, []))  # deep copy
                if self.skip_change_regex:
                    skip_regexes.extend(self.skip_change_regex.splitlines())
                if len(skip_regexes) > 0:
                    new_file_lines = new_file.read_text(errors="ignore").splitlines()
                    old_file_lines = old_file.read_text(errors="ignore").splitlines()
                    log.info("Checking diff")

                    # Only continue if we found something to diff with
                    if len(new_file_lines) > 0 or len(old_file_lines) > 0:
                        log.info("starting..")
                        diffs = difflib.Differ().compare(new_file_lines, old_file_lines)
                        log.info("generated diff ")
                        log.info(f"len: {len(list(diffs))}")
                        lost_lines = [d for d in diffs if d.startswith("-")]

                        # Only continue if there was some diff
                        log.info(f"Found {lost_lines} lines")
                        if len(lost_lines) > 0:
                            matched_lost_lines = []
                            for skip_regex in skip_regexes:
                                for line in lost_lines:
                                    if re.search(skip_regex, line):
                                        matched_lost_lines.append(line)

                            log_msg += f", {len(matched_lost_lines)}/{len(lost_lines)} diff lines matched regex filters"
                            if len(matched_lost_lines) == len(lost_lines):
                                create_file = False
                        else:
                            log_msg += ", no diff found"
                    else:
                        log_msg += ", no text to diff"

        old_fn_relative = Path(old_fn).relative_to(Path.cwd())
        if create_file:
            self.num_img_saved += 1
            log.info(f"Saved: '{old_fn_relative}' ({log_msg})")
        else:
            self.num_img_skipped += 1
            log.debug(f"[dim]Skipped: '{old_fn_relative}' ({log_msg})")

        return create_file

    def save_images(self):
        """Save the images to the specified filenames."""
        if self.aborted:
            return
        if len(self.img_paths) == 0:
            log.warning("Tried to save images with no paths")
            return

        # Set up theme
        terminal_theme = None
        if self.terminal_theme:
            try:
                terminal_theme = getattr(rich.terminal_theme, self.terminal_theme)
            except AttributeError:
                log.error(
                    "[red]Theme '{}' not found![/] Falling back to default for [magenta]{}".format(
                        self.terminal_theme, ", ".join(self.img_paths)
                    )
                )

        # Save image as requested with $IMG_PATHS
        svg_img = None
        png_img = None
        pdf_img = None
        for filename in self.img_paths:

            # Make directories if necessary
            Path(filename).parent.mkdir(parents=True, exist_ok=True)

            # If already made this image, copy it from the last destination
            if filename.lower().endswith(".png") and png_img is not None:
                log.debug(f"Using '{png_img}' for '{filename}'")
                if self._enough_image_difference(png_img, filename):
                    copyfile(png_img, filename)
                continue
            if filename.lower().endswith(".pdf") and pdf_img is not None:
                log.debug(f"Using '{pdf_img}' for '{filename}'")
                if self._enough_image_difference(pdf_img, filename):
                    copyfile(pdf_img, filename)
                continue
            if filename.lower().endswith(".svg") and svg_img is not None:
                log.debug(f"Using '{svg_img}' for '{filename}'")
                if self._enough_image_difference(svg_img, filename):
                    copyfile(svg_img, filename)
                continue

            # Set filenames
            tmp_filename = mkstemp()[1]

            # We always generate an SVG first
            if svg_img is None:
                svg_tmp_filename = mkstemp()[1]
                self.capture_console.save_svg(svg_tmp_filename, title=self.title, theme=terminal_theme)
            else:
                # Use already-generated SVG
                svg_tmp_filename = svg_img

            # Save the SVG image if requested
            if filename.lower().endswith(".svg"):
                if self._enough_image_difference(svg_tmp_filename, filename):
                    copyfile(svg_tmp_filename, filename)
                svg_img = filename

            # Lazy-load PNG / PDF libraries if needed
            if filename.lower().endswith(".png") or filename.lower().endswith(".pdf"):
                try:
                    from cairosvg import svg2pdf, svg2png
                except ImportError as e:
                    log.debug(e)
                    log.error("CairoSVG not installed, cannot convert SVG to PNG or PDF.")
                    log.info("Please install with cairo extra: 'rich-codex[cairo]'")
                    continue
                except OSError as e:
                    log.debug(e)
                    log.error(
                        "⚠️  Missing [link=https://cairosvg.org/documentation/]CairoSVG dependencies[/], "
                        "cannot convert SVG to PNG or PDF. ⚠️\n"
                        f"[red]Skipping image '{filename}'[/]"
                    )
                    continue

                # Convert to PNG if requested
                if filename.lower().endswith(".png"):
                    log.debug(f"Converting SVG '{svg_tmp_filename}' to PNG '{filename}'")
                    svg2png(
                        file_obj=open(svg_tmp_filename, "rb"),
                        write_to=tmp_filename,
                        dpi=300,
                        output_width=4000,
                    )
                    if self._enough_image_difference(tmp_filename, filename):
                        copyfile(tmp_filename, filename)
                        png_img = filename

                # Convert to PDF if requested
                if filename.lower().endswith(".pdf"):
                    log.debug(f"Converting SVG '{svg_tmp_filename}' to PDF '{filename}'")
                    svg2pdf(
                        file_obj=open(svg_tmp_filename, "rb"),
                        write_to=tmp_filename,
                    )
                    if self._enough_image_difference(tmp_filename, filename):
                        copyfile(tmp_filename, filename)
                        pdf_img = filename

            # Delete temprary files
            tmp_path = Path(tmp_filename)
            if tmp_path.is_relative_to(gettempdir()):
                tmp_path.unlink()

        # Delete temporary SVG file - after loop as can be reused
        tmp_svg_path = Path(svg_tmp_filename)
        if tmp_svg_path.is_relative_to(gettempdir()):
            tmp_svg_path.unlink()
