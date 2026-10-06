"""Keep existing German-language regression assertions independent of the host."""

import unittest
from unittest.mock import patch


class GermanTestCase(unittest.TestCase):
    def run(self, result=None):
        with patch("pimpmykernel.i18n._language", "de"):
            return super().run(result)
