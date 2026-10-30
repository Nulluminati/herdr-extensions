from unittest import mock


def fake_opener(returncode=0, stderr=b""):
    """A Popen fake for xdg-open or open: the process exits with `returncode` and writes `stderr`."""
    process = mock.Mock(returncode=returncode)
    process.communicate.return_value = (None, stderr)
    return mock.Mock(return_value=process)
