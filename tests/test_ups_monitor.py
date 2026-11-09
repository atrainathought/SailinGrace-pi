"""ups_monitor.py: INA219 bus-voltage conversion."""
import pytest

pytest.importorskip("smbus2")
import ups_monitor as ups  # noqa: E402


class FakeBus:
    def __init__(self, word):
        self.word = word

    def read_word_data(self, addr, reg):
        assert reg == ups.INA219_REG_BUS_VOLTAGE
        return self.word


def _word_for(volts):
    """What smbus returns for a given bus voltage (little-endian read of a
    big-endian register, voltage in bits 15..3 at 4 mV/LSB)."""
    reg = int(round(volts / 0.004)) << 3
    return ((reg & 0xFF) << 8) | (reg >> 8)


@pytest.mark.parametrize("volts", [8.4, 7.4, 6.4, 6.0])
def test_read_bus_voltage(volts):
    assert ups.read_bus_voltage(FakeBus(_word_for(volts))) == pytest.approx(volts, abs=0.004)
