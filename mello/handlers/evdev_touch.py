"""
Evdev Touch Handler - Direct touch input for KMSDRM mode.

When running with KMSDRM driver (without Wayland), SDL2 doesn't automatically
pick up touch input from evdev devices. This module reads touch events directly
and converts them to pygame mouse events.
"""
import ctypes
import fcntl
import os
import subprocess
import threading
import time
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class _I2cMsg(ctypes.Structure):
    """struct i2c_msg from linux/i2c.h"""
    _fields_ = [('addr', ctypes.c_uint16), ('flags', ctypes.c_uint16),
                ('len', ctypes.c_uint16), ('buf', ctypes.POINTER(ctypes.c_uint8))]


class _I2cRdwr(ctypes.Structure):
    """struct i2c_rdwr_ioctl_data from linux/i2c-dev.h"""
    _fields_ = [('msgs', ctypes.POINTER(_I2cMsg)), ('nmsgs', ctypes.c_uint32)]

# Only import evdev if available (not needed on desktop)
try:
    import evdev
    from evdev import ecodes
    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False
    logger.debug('evdev not available - touch input via SDL only')


class EvdevTouchHandler:
    """Reads touch input directly from evdev and posts pygame events."""

    # The Goodix controller hangs off the DSI panel's own muxed I2C bus, and it
    # sometimes goes deaf while keeping its input node alive. The driver's sysfs
    # entry is named '<bus>-<addr>' ('10-005d'), which is where the probe and the
    # rebind both get their target.
    DRIVER_DIR = '/sys/bus/i2c/drivers/Goodix-TS'
    ID_REGISTER = (0x81, 0x40)          # GT911 product id, reads back b'911\x00'
    I2C_SLAVE_FORCE = 0x0706            # ioctl: address a chip the driver owns
    I2C_RDWR = 0x0707                   # ioctl: combined transfer, no bus release
    I2C_M_RD = 0x0001

    def __init__(self, screen_width: int, screen_height: int):
        self.screen_width = screen_width
        self.screen_height = screen_height
        
        self._device: Optional['evdev.InputDevice'] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._healthy = False
        self._failure_reason: Optional[str] = None
        self._failure_lock = threading.Lock()

        # Wake signal for sleep mode (pygame.event.post from thread
        # doesn't reliably wake pygame.event.wait in KMSDRM mode)
        self.wake_event = threading.Event()

        # Touch state (written from reader thread, read from main thread)
        self._touch_lock = threading.Lock()
        self._touch_x = 0
        self._touch_y = 0
        self._touching = False
        
        # Calibration (touch panel dimensions, detected at start)
        self._touch_max_x = 1279
        self._touch_max_y = 719

        # Last driver entry seen bound, so recovery can bind it back by name
        self._i2c_entry: Optional[str] = None
    
    def start(self) -> bool:
        """Start reading touch events. Returns True if successful."""
        if not EVDEV_AVAILABLE:
            self._mark_failed('evdev not available')
            logger.debug('evdev not available, skipping touch handler')
            return False
        
        # Find touchscreen device
        self._device = self._find_touchscreen()
        if not self._device:
            self._mark_failed('no touchscreen found')
            logger.warning('No touchscreen found')
            return False
        
        logger.info(f'Touch input: {self._device.name} ({self._device.path})')
        
        # Get touch panel dimensions from device
        caps = self._device.capabilities()
        if ecodes.EV_ABS in caps:
            for code, absinfo in caps[ecodes.EV_ABS]:
                if code == ecodes.ABS_X:
                    self._touch_max_x = absinfo.max
                elif code == ecodes.ABS_Y:
                    self._touch_max_y = absinfo.max
        
        logger.info(f'Touch calibration: {self._touch_max_x}x{self._touch_max_y} -> {self.screen_width}x{self.screen_height}')
        
        # Start reader thread
        self._running = True
        self._healthy = True
        self._thread = threading.Thread(
            target=self._read_loop, args=(self._device,), daemon=True
        )
        self._thread.start()
        return True

    def _driver_entry(self) -> Optional[str]:
        """Name of the bound Goodix I2C device, e.g. '10-005d'."""
        try:
            for entry in os.listdir(self.DRIVER_DIR):
                if '-' in entry:
                    self._i2c_entry = entry
                    return entry
        except OSError:
            pass
        return None

    def is_controller_alive(self) -> Optional[bool]:
        """Ask the touch chip for its product id. None when it can't be asked.

        An untouched panel is silent, so silence proves nothing — this probe is
        the only way to tell "nobody touched it" from "the controller went deaf",
        which is what leaves a dark screen that ignores every tap until the plug
        is pulled.

        ponytail: one 4-byte read a minute while asleep, no i2c-tools, no smbus.
        The kernel locks the bus per transfer, so it can't corrupt a driver read.
        """
        if not os.path.isdir(self.DRIVER_DIR):
            return None  # not a Goodix panel (desktop, or a different display)

        entry = self._driver_entry()
        if entry is None:
            return False  # driver loaded but the chip fell off the bus

        fd = None
        try:
            bus, _, addr = entry.partition('-')
            chip = int(addr, 16)
            fd = os.open(f'/dev/i2c-{int(bus)}', os.O_RDWR)
            fcntl.ioctl(fd, self.I2C_SLAVE_FORCE, chip)
            # One combined transaction (write register, repeated START, read).
            # Writing and reading as two transactions releases the bus in
            # between and misses every fourth probe on this panel — measured.
            reg = (ctypes.c_uint8 * 2)(*self.ID_REGISTER)
            out = (ctypes.c_uint8 * 4)()
            msgs = (_I2cMsg * 2)(
                _I2cMsg(chip, 0, 2, reg),
                _I2cMsg(chip, self.I2C_M_RD, 4, out),
            )
            fcntl.ioctl(fd, self.I2C_RDWR, _I2cRdwr(msgs, 2))
            return bytes(out).startswith(b'9')
        except ValueError as e:
            # An unparseable entry means the chip can't be asked, which is not
            # the same as the chip being dead — don't let it drive a rebind.
            logger.warning(f'Touch controller probe skipped: entry={entry!r} ({e})')
            return None
        except OSError as e:
            logger.warning(f'Touch controller probe failed: {e}')
            return False
        finally:
            if fd is not None:
                os.close(fd)

    def recover(self) -> bool:
        """Rebind the touch driver, then re-attach the reader to the new node.

        Same unbind/bind the boot-time touch-fix service does — the only known
        way back from a deaf controller short of a power cycle.
        """
        entry = self._driver_entry() or self._i2c_entry
        if entry is None:
            logger.error('Touch rebind skipped: no Goodix device name known')
            return False

        # Down before the unbind: the reader is about to lose its node with
        # ENODEV, and that expected death must not be reported as a failure —
        # the app answers a touch failure by disabling sleep for good.
        self._running = False

        for action in ('unbind', 'bind'):
            result = subprocess.run(
                ['sudo', 'tee', f'{self.DRIVER_DIR}/{action}'],
                input=entry.encode(), capture_output=True, timeout=10,
            )
            logger.info(f'Touch {action} {entry}: rc={result.returncode}')
            time.sleep(1)  # the driver needs a moment to drop/recreate the node

        return self.restart()

    def restart(self) -> bool:
        """Re-open the touchscreen after the driver was rebound.

        The input node is usually a different eventN after a rebind, so this
        re-runs detection rather than reopening the old path.
        """
        self._running = False  # the old reader thread dies quietly
        if self._device:
            try:
                self._device.close()
            except Exception as e:
                logger.debug(f'Error closing touch device: {e}')
        self._device = None
        with self._touch_lock:
            # A rebind while a finger is down never sees the release. Left True,
            # this reads as a hold that never ends, which overrides bedtime.
            self._touching = False
        with self._failure_lock:
            self._failure_reason = None
        return self.start()

    @property
    def is_touching(self) -> bool:
        """True while a finger is down (used for hold-to-wake during quiet hours)."""
        with self._touch_lock:
            return self._touching

    @property
    def is_available(self) -> bool:
        """True when a touchscreen device was found and the reader is running."""
        return self._healthy and self._device is not None

    @property
    def device_name(self) -> Optional[str]:
        """Human-readable touchscreen name, if detected."""
        return self._device.name if self._device else None

    @property
    def device_path(self) -> Optional[str]:
        """evdev path for the touchscreen, if detected."""
        return self._device.path if self._device else None

    def consume_failure_reason(self) -> Optional[str]:
        """Return and clear the latest touch failure reason."""
        with self._failure_lock:
            reason = self._failure_reason
            self._failure_reason = None
            return reason

    def _mark_failed(self, reason: str):
        """Mark touch wake as unavailable and expose the reason to the app."""
        self._healthy = False
        with self._failure_lock:
            self._failure_reason = reason
    
    def stop(self):
        """Stop reading touch events."""
        self._running = False
        self._healthy = False
        if self._device:
            try:
                self._device.close()
            except Exception as e:
                logger.debug(f'Error closing touch device: {e}')
    
    def _find_touchscreen(self) -> Optional['evdev.InputDevice']:
        """Find the touchscreen device."""
        try:
            devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
            for device in devices:
                # Look for touchscreen by name or capabilities
                name_lower = device.name.lower()
                if 'touch' in name_lower or 'goodix' in name_lower:
                    return device
                # Check for BTN_TOUCH capability
                caps = device.capabilities()
                if ecodes.EV_KEY in caps:
                    if ecodes.BTN_TOUCH in caps[ecodes.EV_KEY]:
                        return device
        except Exception as e:
            logger.warning(f'Error finding touchscreen: {e}')
        return None
    
    def _scale_coordinates(self, touch_x: int, touch_y: int) -> Tuple[int, int]:
        """Scale touch coordinates to screen coordinates.
        
        Direct mapping from touch panel to screen coordinates.
        """
        screen_x = int(touch_x * self.screen_width / self._touch_max_x)
        screen_y = int(touch_y * self.screen_height / self._touch_max_y)
        
        # Clamp to screen bounds
        screen_x = max(0, min(self.screen_width - 1, screen_x))
        screen_y = max(0, min(self.screen_height - 1, screen_y))
        
        return screen_x, screen_y
    
    def _read_loop(self, device):
        """Read touch events in background thread."""
        import pygame

        try:
            for event in device.read_loop():
                if not self._running:
                    break
                
                # Handle touch position
                if event.type == ecodes.EV_ABS:
                    with self._touch_lock:
                        if event.code == ecodes.ABS_X or event.code == ecodes.ABS_MT_POSITION_X:
                            self._touch_x = event.value
                        elif event.code == ecodes.ABS_Y or event.code == ecodes.ABS_MT_POSITION_Y:
                            self._touch_y = event.value

                # Handle touch down/up
                elif event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
                    with self._touch_lock:
                        pos = self._scale_coordinates(self._touch_x, self._touch_y)

                    if event.value == 1:  # Touch down
                        with self._touch_lock:
                            self._touching = True
                        self.wake_event.set()
                        pygame.event.post(pygame.event.Event(
                            pygame.MOUSEBUTTONDOWN,
                            {'pos': pos, 'button': 1}
                        ))
                        logger.debug(f'Touch DOWN at {pos}')

                    elif event.value == 0:  # Touch up
                        with self._touch_lock:
                            self._touching = False
                        pygame.event.post(pygame.event.Event(
                            pygame.MOUSEBUTTONUP,
                            {'pos': pos, 'button': 1}
                        ))
                        logger.debug(f'Touch UP at {pos}')

                # Handle touch move (SYN_REPORT indicates end of event batch)
                elif event.type == ecodes.EV_SYN:
                    with self._touch_lock:
                        touching = self._touching
                        if touching:
                            pos = self._scale_coordinates(self._touch_x, self._touch_y)
                    if touching:
                        pygame.event.post(pygame.event.Event(
                            pygame.MOUSEMOTION,
                            {'pos': pos, 'rel': (0, 0), 'buttons': (1, 0, 0)}
                        ))
        
        except Exception as e:
            # `device is self._device` keeps a reader that lost its node during
            # a rebind from reporting the failure its replacement already fixed.
            if self._running and device is self._device:
                self._mark_failed(f'touch read error: {e}')
                logger.error(f'Touch read error: {e}')
        else:
            if self._running and device is self._device:
                self._mark_failed('touch read loop exited')
                logger.error('Touch read loop exited unexpectedly')
        
        logger.debug('Touch handler stopped')
