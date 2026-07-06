import datetime
import traceback

from colorama import Fore, Style


class _LOGGING:
    class cowolors:
        def __init__(self) -> None:
            self.reset = Style.RESET_ALL
            self.timestamp = f"{Style.BRIGHT}{Fore.LIGHTBLACK_EX}"
            self.normal_message = Fore.WHITE

            self.info_logs = Fore.CYAN
            self.cog_logs = Fore.BLUE
            self.command_logs = Fore.BLUE
            self.success_logs = Fore.LIGHTGREEN_EX
            self.error_logs = Fore.RED
            self.warn_logs = "\033[38;2;255;165;0m"  # orange

            self.item_name = Fore.LIGHTBLUE_EX
            self.user_name = Fore.LIGHTCYAN_EX

    COLORS = cowolors()

    def print(self, *args, **kwargs) -> None:
        timestamp = (
            f"{self.COLORS.timestamp}"
            f"[{datetime.datetime.now(datetime.timezone.utc).strftime('%Y/%m/%d %H:%M:%S.%f')[:-3]} UTC]"
            f"{self.COLORS.reset}"
        )
        if args:
            args = (timestamp + " " + str(args[0]),) + args[1:]
        else:
            args = (timestamp,)
        print(*args, **kwargs)

    def _tagged(self, tag: str, color: str, args: tuple, kwargs: dict) -> None:
        prefix = f"{color}[{tag}]{self.COLORS.normal_message}"
        if args:
            args = (prefix + " " + str(args[0]),) + args[1:]
        else:
            args = (prefix,)
        self.print(*args, **kwargs)

    def infoprint(self, *args, **kwargs) -> None:
        self._tagged("INFO", self.COLORS.info_logs, args, kwargs)

    def warnprint(self, *args, **kwargs) -> None:
        self._tagged("WARN", self.COLORS.warn_logs, args, kwargs)

    def errorprint(self, *args, **kwargs) -> None:
        self._tagged("ERROR", self.COLORS.error_logs, args, kwargs)

    def successprint(self, *args, **kwargs) -> None:
        self._tagged("SUCCESS", self.COLORS.success_logs, args, kwargs)

    def tracebackprint(self, error: Exception) -> None:
        separator_line = "-" * 60
        traceback_lines = traceback.format_exception(error)
        print(separator_line)
        errortimestamp = (
            datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y/%m/%d %H:%M:%S.%f"
            )[:-3]
            + " UTC"
        )
        for line in traceback_lines:
            for subline in line.split("\n"):
                self.print(
                    f"{self.COLORS.timestamp}[{errortimestamp}]{self.COLORS.reset} "
                    f"{self.COLORS.error_logs}[ERROR]{self.COLORS.normal_message} {subline}"
                )
        print(separator_line)


LOGGING = _LOGGING()
