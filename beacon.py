# Beacon eddy current scanner support
#
# Copyright (C) 2020-2023 Matt Baker <baker.matt.j@gmail.com>
# Copyright (C) 2020-2023 Lasse Dalegaard <dalegaard@gmail.com>
# Copyright (C) 2023 Beacon <beacon3d.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import threading
import multiprocessing
import subprocess
import os
import importlib
import traceback
import logging
import chelper
import pins
import math
import time
import queue
import struct
import numpy as np
import copy
import collections
import itertools
from numpy.polynomial import Polynomial
from . import manual_probe
from . import probe
from . import bed_mesh
from . import thermistor
from . import adxl345
from .homing import HomingMove
from mcu import MCU, MCU_trsync
from clocksync import SecondarySync
import msgproto

STREAM_BUFFER_LIMIT_DEFAULT = 100
STREAM_TIMEOUT = 1.0
API_DUMP_FIELDS = ["dist", "temp", "pos", "freq", "time"]

TRSYNC_TIMEOUT_DEFAULT = 0.025


class BeaconProbe:
    def __init__(self, config, sensor_id):
        self.id = sensor_id
        self.printer = printer = config.get_printer()
        self.reactor = printer.get_reactor()
        self.name = config.get_name()
        self.gcode = printer.lookup_object("gcode")

        self.speed = config.getfloat("speed", 5.0, above=0.0)
        self.lift_speed = config.getfloat("lift_speed", self.speed, above=0.0)
        self.backlash_comp = config.getfloat("backlash_comp", 0.5)

        self.x_offset = config.getfloat("x_offset", 0.0)
        self.y_offset = config.getfloat("y_offset", 0.0)

        self.trigger_distance = config.getfloat("trigger_distance", 2.0)
        self.trigger_dive_threshold = config.getfloat("trigger_dive_threshold", 1.0)
        self.trigger_hysteresis = config.getfloat("trigger_hysteresis", 0.006)
        self.z_settling_time = config.getint("z_settling_time", 5, minval=0)
        self.default_probe_method = config.getchoice(
            "default_probe_method",
            {"contact": "contact", "proximity": "proximity"},
            "proximity",
        )

        # If using paper for calibration, this would be .1mm
        self.cal_nozzle_z = config.getfloat("cal_nozzle_z", 0.1)
        self.cal_floor = config.getfloat("cal_floor", 0.2)
        self.cal_ceil = config.getfloat("cal_ceil", 5.0)
        self.cal_speed = config.getfloat("cal_speed", 1.0)
        self.cal_move_speed = config.getfloat("cal_move_speed", 10.0)

        self.autocal_max_speed = config.getfloat("autocal_max_speed", 10)
        self.autocal_speed = config.getfloat("autocal_speed", 3)
        # [CUSTOM] Added approach speed for fast initial proximity move
        self.approach_speed = config.getfloat("approach_speed", 3)
        self.autocal_accel = config.getfloat("autocal_accel", 100)
        self.autocal_retract_dist = config.getfloat("autocal_retract_dist", 2)
        self.autocal_retract_speed = config.getfloat("autocal_retract_speed", 10)
        self.autocal_sample_count = config.getfloat("autocal_sample_count", 3)
        self.autocal_tolerance = config.getfloat("autocal_tolerance", 0.008)
        self.autocal_max_retries = config.getfloat("autocal_max_retries", 3)

        self.contact_latency_min = config.getint("contact_latency_min", 0)
        self.contact_sensitivity = config.getint("contact_sensitivity", 0)

        self.trsync_timeout = config.getfloat(
            "trsync_timeout", TRSYNC_TIMEOUT_DEFAULT, maxval=0.25
        )

        self.skip_firmware_version_check = config.getboolean(
            "skip_firmware_version_check", False
        )

        # Load models
        self.model = None
        self.models = {}
        self.model_temp_builder = BeaconTempModelBuilder.load(config)
        self.model_temp = None
        self.fmin = None
        self.default_model_name = config.get("default_model_name", "default")
        self.model_manager = ModelManager(self)

        # Temperature sensor integration
        self.last_temp = 0
        self.last_mcu_temp = None
        self.measured_min = 99999999.0
        self.measured_max = 0.0
        self.mcu_temp = None
        self.thermistor = None

        self.last_sample = None
        self.last_received_sample = None
        self.last_z_result = 0
        self.last_probe_position = (0, 0)
        self.last_probe_result = None
        self.last_offset_result = None
        self.last_poke_result = None
        self.last_contact_msg = None
        self.hardware_failure = None

        self.mesh_helper = BeaconMeshHelper.create(self, config)
        self.homing_helper = BeaconHomingHelper.create(self, config)
        self.accel_helper = None
        self.accel_config = BeaconAccelConfig(config)

        self._stream_en = 0
        self._stream_timeout_timer = self.reactor.register_timer(self._stream_timeout)
        self._stream_callbacks = {}
        self._stream_latency_requests = {}
        self._stream_buffer = []
        self._stream_buffer_count = 0
        self._stream_buffer_limit = STREAM_BUFFER_LIMIT_DEFAULT
        self._stream_buffer_limit_new = self._stream_buffer_limit
        self._stream_samples_queue = queue.Queue()
        self._stream_flush_event = threading.Event()
        self._log_stream = None
        self._data_filter = AlphaBetaFilter(
            config.getfloat("filter_alpha", 0.5),
            config.getfloat("filter_beta", 0.000001),
        )
        self.trapq = None
        self.mod_axis_twist_comp = None
        self.get_z_compensation_value = lambda pos: 0.0

        mainsync = printer.lookup_object("mcu")._clocksync
        self._mcu = MCU(config, SecondarySync(self.reactor, mainsync))
        orig_stats = self._mcu.stats

        def beacon_mcu_stats(eventtime):
            show, value = orig_stats(eventtime)
            value += " " + self._extend_stats()
            return show, value

        self._mcu.stats = beacon_mcu_stats
        printer.add_object("mcu " + self.name, self._mcu)
        self.cmd_queue = self._mcu.alloc_command_queue()
        self._endstop_shared = BeaconEndstopShared(self)
        self.mcu_probe = BeaconEndstopWrapper(self)
        self.mcu_contact_probe = BeaconContactEndstopWrapper(self, config)
        self._current_probe = "proximity"

        self.beacon_stream_cmd = None
        self.beacon_set_threshold = None
        self.beacon_home_cmd = None
        self.beacon_stop_home_cmd = None
        self.beacon_nvm_read_cmd = None
        self.beacon_contact_home_cmd = None
        self.beacon_contact_query_cmd = None
        self.beacon_contact_stop_home_cmd = None
        self.beacon_contact_set_latency_min_cmd = None
        self.beacon_contact_set_sensitivity_cmd = None

        # Register z_virtual_endstop
        register_as_probe = config.getboolean(
            "register_as_probe", sensor_id.is_unnamed()
        )
        if register_as_probe:
            printer.lookup_object("pins").register_chip("probe", self)

        # Register event handlers
        printer.register_event_handler("klippy:connect", self._handle_connect)
        printer.register_event_handler("klippy:shutdown", self.force_stop_streaming)
        self._mcu.register_config_callback(self._build_config)
        self._mcu.register_response(self._handle_beacon_data, "beacon_data")
        self._mcu.register_response(self._handle_beacon_status, "beacon_status")
        self._mcu.register_response(self._handle_beacon_contact, "beacon_contact")

        # Register webhooks
        self._api_dump = APIDumpHelper(
            printer,
            lambda: self.streaming_session(self._api_dump_callback, latency=50),
            lambda stream: stream.stop(),
            None,
        )
        sensor_id.register_endpoint("beacon/status", self._handle_req_status)
        sensor_id.register_endpoint("beacon/dump", self._handle_req_dump)

        # Register gcode commands
        sensor_id.register_command(
            "BEACON_STREAM", self.cmd_BEACON_STREAM, desc=self.cmd_BEACON_STREAM_help
        )
        sensor_id.register_command(
            "BEACON_QUERY", self.cmd_BEACON_QUERY, desc=self.cmd_BEACON_QUERY_help
        )
        sensor_id.register_command(
            "BEACON_CALIBRATE",
            self.cmd_BEACON_CALIBRATE,
            desc=self.cmd_BEACON_CALIBRATE_help,
        )
        sensor_id.register_command(
            "BEACON_ESTIMATE_BACKLASH",
            self.cmd_BEACON_ESTIMATE_BACKLASH,
            desc=self.cmd_BEACON_ESTIMATE_BACKLASH_help,
        )
        sensor_id.register_command("PROBE", self.cmd_PROBE, desc=self.cmd_PROBE_help)
        sensor_id.register_command(
            "PROBE_ACCURACY", self.cmd_PROBE_ACCURACY, desc=self.cmd_PROBE_ACCURACY_help
        )
        sensor_id.register_command(
            "Z_OFFSET_APPLY_PROBE",
            self.cmd_Z_OFFSET_APPLY_PROBE,
            desc=self.cmd_Z_OFFSET_APPLY_PROBE_help,
        )
        sensor_id.register_command(
            "BEACON_POKE", self.cmd_BEACON_POKE, desc=self.cmd_BEACON_POKE_help
        )
        sensor_id.register_command(
            "BEACON_AUTO_CALIBRATE",
            self.cmd_BEACON_AUTO_CALIBRATE,
            desc=self.cmd_BEACON_AUTO_CALIBRATE_help,
        )
        sensor_id.register_command(
            "BEACON_OFFSET_COMPARE",
            self.cmd_BEACON_OFFSET_COMPARE,
            desc=self.cmd_BEACON_OFFSET_COMPARE_help,
        )
        if sensor_id.is_unnamed():
            self._hook_probing_gcode(config, "z_tilt", "Z_TILT_ADJUST")
            self._hook_probing_gcode(config, "quad_gantry_level", "QUAD_GANTRY_LEVEL")
            self._hook_probing_gcode(config, "screws_tilt_adjust", "SCREWS_TILT_ADJUST")
            self._hook_probing_gcode(config, "delta_calibrate", "DELTA_CALIBRATE")

        # Qidi Klipper compatibility
        self.vibrate = 0

    # Event handlers

    def _handle_connect(self):
        self.phoming = self.printer.lookup_object("homing")
        self.mod_axis_twist_comp = self.printer.lookup_object(
            "axis_twist_compensation", None
        )
        if self.mod_axis_twist_comp:
            if hasattr(self.mod_axis_twist_comp, "get_z_compensation_value"):
                self.get_z_compensation_value = (
                    lambda pos: self.mod_axis_twist_comp.get_z_compensation_value(pos)
                )
            elif hasattr(manual_probe, "ProbeResult"):

                def _update_compensation(pos):
                    cpos = [self.compat_create_probe_result(pos)]
                    bed_z = cpos[0].bed_z
                    self.mod_axis_twist_comp._update_z_compensation_value(cpos)
                    return cpos[0].bed_z - bed_z

                self.get_z_compensation_value = _update_compensation
            else:

                def _update_compensation(pos):
                    cpos = list(pos)
                    self.mod_axis_twist_comp._update_z_compensation_value(cpos)
                    return cpos[2] - pos[2]

                self.get_z_compensation_value = _update_compensation

        if self.model is None:
            self.model = self.models.get(self.default_model_name, None)

    def _check_mcu_version(self):
        if self.skip_firmware_version_check:
            return ""
        updater = os.path.join(self.id.tracker.home_dir(), "update_firmware.py")
        if not os.path.exists(updater):
            logging.info(
                "Could not find Beacon firmware update script, won't check for update."
            )
            return ""
        serialport = self.compat_serial_port(self._mcu)

        parent_conn, child_conn = multiprocessing.Pipe()

        def do():
            try:
                output = subprocess.check_output(
                    [updater, "check", serialport], universal_newlines=True
                )
                child_conn.send((False, output.strip()))
            except Exception:
                child_conn.send((True, traceback.format_exc()))
            child_conn.close()

        child = multiprocessing.Process(target=do)
        child.daemon = True
        child.start()
        eventtime = self.reactor.monotonic()
        while child.is_alive():
            eventtime = self.reactor.pause(eventtime + 0.1)
        (is_err, result) = parent_conn.recv()
        child.join()
        parent_conn.close()
        if is_err:
            logging.info("Executing Beacon update script failed: %s", result)
        elif result != "":
            self.gcode.respond_raw("!! " + result + "\n")
            pconfig = self.printer.lookup_object("configfile")
            try:
                pconfig.runtime_warning(result)
            except AttributeError:
                logging.info(result)
            return result
        return ""

    def _build_config(self):
        version_info = self._check_mcu_version()

        try:
            self.beacon_stream_cmd = self._mcu.lookup_command(
                "beacon_stream en=%u", cq=self.cmd_queue
            )
            self.beacon_set_threshold = self._mcu.lookup_command(
                "beacon_set_threshold trigger=%u untrigger=%u", cq=self.cmd_queue
            )
            self.beacon_home_cmd = self._mcu.lookup_command(
                "beacon_home trsync_oid=%c trigger_reason=%c trigger_invert=%c",
                cq=self.cmd_queue,
            )
            self.beacon_stop_home_cmd = self._mcu.lookup_command(
                "beacon_stop_home", cq=self.cmd_queue
            )
            self.beacon_nvm_read_cmd = self._mcu.lookup_query_command(
                "beacon_nvm_read len=%c offset=%hu",
                "beacon_nvm_data bytes=%*s offset=%hu",
                cq=self.cmd_queue,
            )
            self.beacon_contact_home_cmd = self._mcu.lookup_command(
                "beacon_contact_home trsync_oid=%c trigger_reason=%c trigger_type=%c",
                cq=self.cmd_queue,
            )
            self.beacon_contact_query_cmd = self._mcu.lookup_query_command(
                "beacon_contact_query",
                "beacon_contact_state triggered=%c detect_clock=%u",
                cq=self.cmd_queue,
            )
            self.beacon_contact_stop_home_cmd = self._mcu.lookup_command(
                "beacon_contact_stop_home",
                cq=self.cmd_queue,
            )
            try:
                self.beacon_contact_set_latency_min_cmd = self._mcu.lookup_command(
                    "beacon_contact_set_latency_min latency_min=%c",
                    cq=self.cmd_queue,
                )
            except msgproto.error:
                pass
            try:
                self.beacon_contact_set_sensitivity_cmd = self._mcu.lookup_command(
                    "beacon_contact_set_sensitivity sensitivity=%c",
                    cq=self.cmd_queue,
                )
            except msgproto.error:
                pass

            constants = self._mcu.get_constants()

            self._mcu_freq = self._mcu.get_constant_float("CLOCK_FREQ")

            self.inv_adc_max = 1.0 / constants.get("ADC_MAX")
            self.temp_smooth_count = constants.get("BEACON_ADC_SMOOTH_COUNT")
            self.thermistor = thermistor.Thermistor(10000.0, 0.0)
            self.thermistor.setup_coefficients_beta(25.0, 47000.0, 4101.0)

            self.toolhead = self.printer.lookup_object("toolhead")
            self.kinematics = self.toolhead.get_kinematics()
            self.trapq = self.toolhead.get_trapq()

            self.mcu_temp = BeaconMCUTempHelper.build_with_nvm(self)
            self.model_temp = self.model_temp_builder.build_with_nvm(self)
            if self.model_temp:
                self.fmin = self.model_temp.fmin
            if self.model is None:
                self.model = self.models.get(self.default_model_name, None)
            if self.model:
                self._apply_threshold()

            if self.beacon_stream_cmd is not None:
                self.beacon_stream_cmd.send([1 if self._stream_en else 0])
            if self._stream_en:
                curtime = self.reactor.monotonic()
                self.reactor.update_timer(
                    self._stream_timeout_timer, curtime + STREAM_TIMEOUT
                )
            else:
                self.reactor.update_timer(
                    self._stream_timeout_timer, self.reactor.NEVER
                )

            if constants.get("BEACON_HAS_ACCEL", 0) == 1:
                logging.info("Enabling Beacon accelerometer")
                if self.accel_helper is None:
                    self.accel_helper = BeaconAccelHelper(
                        self, self.accel_config, constants
                    )
                else:
                    self.accel_helper.reinit(constants)

        except msgproto.error as e:
            if version_info != "":
                raise msgproto.error(version_info + "\n\n" + str(e))
            raise

    def _extend_stats(self):
        parts = [
            "coil_temp=%.1f" % (self.last_temp,),
            "refs=%d" % (self._stream_en,),
        ]
        if self.last_mcu_temp is not None:
            (mcu_temp, supply_voltage) = self.last_mcu_temp
            parts.append("mcu_temp=%.2f" % (mcu_temp,))
            parts.append("supply_voltage=%.3f" % (supply_voltage,))

        return " ".join(parts)

    def _api_dump_callback(self, sample):
        tmp = [sample.get(key, None) for key in API_DUMP_FIELDS]
        self._api_dump.buffer.append(tmp)

    # Virtual endstop

    def setup_pin(self, pin_type, pin_params):
        if pin_type != "endstop" or pin_params["pin"] != "z_virtual_endstop":
            raise pins.error("Probe virtual endstop only useful as endstop pin")
        if pin_params["invert"] or pin_params["pullup"]:
            raise pins.error("Can not pullup/invert probe virtual endstop")
        return self.mcu_probe

    # Probe interface

    def multi_probe_begin(self):
        self._start_streaming()

    def multi_probe_end(self):
        self._stop_streaming()

    def get_offsets(self, _gcmd=None):
        if self._current_probe == "contact":
            return 0, 0, 0
        else:
            return self.x_offset, self.y_offset, self.trigger_distance

    def get_lift_speed(self, gcmd=None):
        if gcmd is not None:
            return gcmd.get_float("LIFT_SPEED", self.lift_speed, above=0.0)
        return self.lift_speed

    def run_probe(self, gcmd):
        method = gcmd.get("PROBE_METHOD", self.default_probe_method).lower()
        self._current_probe = method
        if method == "proximity":
            return self._run_probe_proximity(gcmd)
        elif method == "contact":
            self._start_streaming()
            try:
                return self._run_probe_contact(gcmd)
            finally:
                self._stop_streaming()
        else:
            raise gcmd.error("Invalid PROBE_METHOD, valid choices: proximity, contact")

    def _move_to_probing_height(self, speed):
        target = self.trigger_distance
        top = target + self.backlash_comp
        cur_z = self.toolhead.get_position()[2]
        if cur_z < top:
            self.toolhead.manual_move([None, None, top], speed)
        self.toolhead.manual_move([None, None, target], speed)
        self.toolhead.wait_moves()

    def _run_probe_proximity(self, gcmd):
        if self.model is None:
            raise self.printer.command_error("No Beacon model loaded")

        speed = gcmd.get_float("PROBE_SPEED", self.speed, above=0.0)
        allow_faulty = gcmd.get_int("ALLOW_FAULTY_COORDINATE", 0) != 0
        toolhead = self.printer.lookup_object("toolhead")
        curtime = self.reactor.monotonic()
        if "z" not in toolhead.get_status(curtime)["homed_axes"]:
            raise self.printer.command_error("Must home before probe")

        self._start_streaming()
        try:
            return self._probe(speed, allow_faulty=allow_faulty)
        finally:
            self._stop_streaming()

    def _probing_move_to_probing_height(self, speed):
        curtime = self.reactor.monotonic()
        status = self.kinematics.get_status(curtime)
        pos = self.toolhead.get_position()
        pos[2] = status["axis_minimum"][2]
        try:
            self.phoming.probing_move(self.mcu_probe, pos, speed)
        except self.printer.command_error as e:
            reason = str(e)
            if "Timeout during probing move" in reason:
                reason += probe.HINT_TIMEOUT
            raise self.printer.command_error(reason)

    def _probe(self, speed, num_samples=10, allow_faulty=False):
        target = self.trigger_distance
        tdt = self.trigger_dive_threshold
        (dist, samples) = self._sample(5, num_samples)

        x, y = samples[0]["pos"][0:2]
        if self._is_faulty_coordinate(x, y, True):
            msg = "Probing within a faulty area"
            if not allow_faulty:
                raise self.printer.command_error(msg)
            else:
                self.gcode.respond_raw("!! " + msg + "\n")

        if dist > target + tdt:
            # If we are above the dive threshold right now, we'll need to
            # do probing move and then re-measure
            self._probing_move_to_probing_height(speed)
            (dist, samples) = self._sample(self.z_settling_time, num_samples)
        elif math.isinf(dist) and dist < 0:
            # We were below the valid range of the model
            msg = "Attempted to probe with Beacon below calibrated model range"
            raise self.printer.command_error(msg)
        elif self.toolhead.get_position()[2] < target - tdt:
            # We are below the probing target height, we'll move to the
            # correct height and take a new sample.
            self._move_to_probing_height(speed)
            (dist, samples) = self._sample(self.z_settling_time, num_samples)

        pos = samples[0]["pos"]

        self.gcode.respond_info(
            "probe at %.3f,%.3f,%.3f is z=%.6f" % (pos[0], pos[1], pos[2], dist)
        )

        return [pos[0], pos[1], pos[2] + target - dist]

    def _run_probe_contact(self, gcmd):
        self.toolhead.wait_moves()
        speed = gcmd.get_float(
            "PROBE_SPEED", self.autocal_speed, above=0.0, maxval=self.autocal_max_speed
        )
        lift_speed = self.get_lift_speed(gcmd)
        sample_count = gcmd.get_int("SAMPLES", self.autocal_sample_count, minval=1)
        retract_dist = gcmd.get_float(
            "SAMPLE_RETRACT_DIST", self.autocal_retract_dist, minval=1
        )
        tolerance = gcmd.get_float(
            "SAMPLES_TOLERANCE", self.autocal_tolerance, above=0.0
        )
        max_retries = gcmd.get_int(
            "SAMPLES_TOLERANCE_RETRIES", self.autocal_max_retries, minval=0
        )
        samples_result = gcmd.get("SAMPLES_RESULT", "mean")
        drop_n = gcmd.get_int("SAMPLES_DROP", 0, minval=0)
        retries = 0
        samples = []

        posxy = self.toolhead.get_position()[:2]

        self.mcu_contact_probe.activate_gcode.run_gcode_from_command()
        try:
            while len(samples) < sample_count:
                pos = self._probe_contact(speed)
                self.toolhead.manual_move(posxy + [pos[2] + retract_dist], lift_speed)
                if drop_n > 0:
                    drop_n -= 1
                    continue
                samples.append(pos[2])
                spread = max(samples) - min(samples)
                if spread > tolerance:
                    if retries >= max_retries:
                        raise gcmd.error("Probe samples exceed sample_tolerance")
                    gcmd.respond_info("Probe samples exceed tolerance. Retrying...")
                    samples = []
                    retries += 1
            if samples_result == "median":
                return posxy + [median(samples)]
            else:
                return posxy + [float(np.mean(samples))]
        finally:
            self.mcu_contact_probe.deactivate_gcode.run_gcode_from_command()

    def _probe_contact(self, speed):
        self.toolhead.get_last_move_time()
        self._sample_async()
        start_pos = self.toolhead.get_position()
        hmove = HomingMove(self.printer, [(self.mcu_contact_probe, "contact")])
        pos = start_pos[:]
        pos[2] = -2
        try:
            epos = hmove.homing_move(pos, speed, probe_pos=True)[:3]
        except self.printer.command_error as e:
            if self.printer.is_shutdown():
                reason = "Probing failed due to printer shutdown"
            else:
                reason = str(e)
                if "Timeout during probing move" in reason:
                    reason += probe.HINT_TIMEOUT
            raise self.printer.command_error(reason)
        epos[2] += self.get_z_compensation_value(pos)
        self.gcode.respond_info(
            "probe at %.3f,%.3f is z=%.6f" % (epos[0], epos[1], epos[2])
        )
        return epos[:3]

    # Accelerometer interface

    def start_internal_client(self):
        if not self.accel_helper:
            msg = "This Beacon has no accelerometer"
            raise self.printer.command_error(msg)
        return self.accel_helper.start_internal_client()

    # Calibration routines

    def _start_calibration(self, gcmd):
        allow_faulty = gcmd.get_int("ALLOW_FAULTY_COORDINATE", 0) != 0
        nozzle_z = gcmd.get_float("NOZZLE_Z", self.cal_nozzle_z)
        if gcmd.get("SKIP_MANUAL_PROBE", None) is not None:
            kin = self.kinematics
            kin_spos = {
                s.get_name(): s.get_commanded_position() for s in kin.get_steppers()
            }
            kin_pos = kin.calc_position(kin_spos)
            if self._is_faulty_coordinate(kin_pos[0], kin_pos[1]):
                msg = "Calibrating within a faulty area"
                if not allow_faulty:
                    raise gcmd.error(msg)
                else:
                    gcmd.respond_raw("!! " + msg + "\n")
            self._calibrate(gcmd, kin_pos, nozzle_z, False)
        else:
            curtime = self.printer.get_reactor().monotonic()
            kin_status = self.toolhead.get_status(curtime)
            if "xy" not in kin_status["homed_axes"]:
                raise self.printer.command_error("Must home X and Y before calibration")

            kin_pos = self.toolhead.get_position()
            if self._is_faulty_coordinate(kin_pos[0], kin_pos[1]):
                msg = "Calibrating within a faulty area"
                if not allow_faulty:
                    raise gcmd.error(msg)
                else:
                    gcmd.respond_raw("!! " + msg + "\n")

            forced_z = False
            if "z" not in kin_status["homed_axes"]:
                self.toolhead.get_last_move_time()
                pos = self.toolhead.get_position()
                pos[2] = (
                    kin_status["axis_maximum"][2]
                    - 2.0
                    - gcmd.get_float("CEIL", self.cal_ceil)
                )
                self.compat_toolhead_set_position_homing_z(self.toolhead, pos)
                forced_z = True

            def cb(kin_pos):
                return self._calibrate(gcmd, kin_pos, nozzle_z, forced_z)

            manual_probe.ManualProbeHelper(self.printer, gcmd, cb)

    def _calibrate(self, gcmd, kin_pos, cal_nozzle_z, forced_z, is_auto=False):
        if kin_pos is None:
            if forced_z:
                kin = self.kinematics
                self.compat_kin_note_z_not_homed(kin)
            return

        gcmd.respond_info("Beacon calibration starting")
        cal_floor = gcmd.get_float("FLOOR", self.cal_floor)
        cal_ceil = gcmd.get_float("CEIL", self.cal_ceil)
        cal_speed = gcmd.get_float("DESCEND_SPEED", self.cal_speed)
        move_speed = gcmd.get_float("MOVE_SPEED", self.cal_move_speed)
        model_name = gcmd.get("MODEL_NAME", "default")

        toolhead = self.toolhead
        toolhead.wait_moves()

        # Move coordinate system to nozzle location
        self.toolhead.get_last_move_time()
        curpos = toolhead.get_position()
        curpos[2] = cal_nozzle_z
        toolhead.set_position(curpos)

        # Move over to probe coordinate and pull out backlash
        curpos[2] = cal_ceil + self.backlash_comp
        toolhead.manual_move(curpos, move_speed)  # Up
        curpos[0] -= self.x_offset
        curpos[1] -= self.y_offset
        toolhead.manual_move(curpos, move_speed)  # Over
        curpos[2] = cal_ceil
        toolhead.manual_move(curpos, move_speed)  # Down
        toolhead.wait_moves()

        samples = []

        def cb(sample):
            samples.append(sample)

        # Descend while sampling
        toolhead.flush_step_generation()
        try:
            self._start_streaming()
            self._sample_printtime_sync(50)
            with self.streaming_session(cb):
                self._sample_printtime_sync(50)
                toolhead.dwell(0.250)
                curpos[2] = cal_floor
                toolhead.manual_move(curpos, cal_speed)
                toolhead.flush_step_generation()
                self._sample_printtime_sync(50)
        finally:
            self._stop_streaming()

        # Fit the sampled data
        z_offset = [s["pos"][2] for s in samples]
        freq = [s["freq"] for s in samples]
        temp = [s["temp"] for s in samples]
        inv_freq = [1 / f for f in freq]
        poly = Polynomial.fit(inv_freq, z_offset, 9)
        temp_median = median(temp)
        self.model = BeaconModel(
            model_name, self, poly, temp_median, min(z_offset), max(z_offset)
        )
        self.models[self.model.name] = self.model
        self.model.save(self, not is_auto)
        self._apply_threshold()

        # Dump calibration curve
        fn = "/tmp/beacon-calibrate-" + time.strftime("%Y%m%d_%H%M%S") + ".csv"
        with open(fn, "w") as f:
            f.write("freq,z,temp\n")
            for i in range(len(freq)):
                f.write("%.5f,%.5f,%.3f\n" % (freq[i], z_offset[i], temp[i]))

        gcmd.respond_info(
            "Beacon calibrated at %.3f,%.3f from "
            "%.3f to %.3f, speed %.2f mm/s, temp %.2fC"
            % (curpos[0], curpos[1], cal_floor, cal_ceil, cal_speed, temp_median)
        )

    # Internal

    def _update_thresholds(self, moving_up=False):
        self.trigger_freq = self.dist_to_freq(self.trigger_distance, self.last_temp)
        self.untrigger_freq = self.trigger_freq * (1 - self.trigger_hysteresis)

    def _apply_threshold(self, moving_up=False):
        self._update_thresholds()
        trigger_c = int(self.freq_to_count(self.trigger_freq))
        untrigger_c = int(self.freq_to_count(self.untrigger_freq))
        if self.beacon_set_threshold is not None:
            self.beacon_set_threshold.send([trigger_c, untrigger_c])

    def _register_model(self, name, model):
        if name in self.models:
            raise self.printer.config_error(
                "Multiple Beacon models with samename '%s'" % (name,)
            )
        self.models[name] = model

    def _is_faulty_coordinate(self, x, y, add_offsets=False):
        if not self.mesh_helper:
            return False
        return self.mesh_helper._is_faulty_coordinate(x, y, add_offsets)

    def _handle_beacon_status(self, params):
        if self.mcu_temp is not None:
            self.last_mcu_temp = self.mcu_temp.compensate(
                self, params["mcu_temp"], params["supply_voltage"]
            )
        if self.thermistor is not None:
            self.last_temp = self.thermistor.calc_temp(
                params["coil_temp"] / self.temp_smooth_count * self.inv_adc_max
            )

    def _handle_beacon_contact(self, params):
        self.last_contact_msg = params

    def _hook_probing_gcode(self, config, module, cmd):
        if not config.has_section(module):
            return
        section = config.getsection(module)
        mod = self.printer.load_object(section, module)
        if mod is None:
            return
        orig = self.gcode.register_command(cmd, None)

        def cb(gcmd):
            self._current_probe = gcmd.get(
                "PROBE_METHOD", self.default_probe_method
            ).lower()
            return orig(gcmd)

        self.gcode.register_command(cmd, cb)

    # Streaming mode

    def _check_hardware(self, sample):
        if not self.hardware_failure:
            msg = None
            if sample["data"] == 0xFFFFFFF:
                msg = "coil is shorted or not connected"
            elif self.fmin is not None and sample["freq"] > 1.35 * self.fmin:
                msg = "coil expected max frequency exceeded"
            if msg:
                msg = "Beacon hardware issue: " + msg
                self.hardware_failure = msg
                logging.error(msg)
                if self._stream_en:
                    self.printer.invoke_shutdown(msg)
                else:
                    self.gcode.respond_raw("!! " + msg + "\n")
        elif self._stream_en:
            self.printer.invoke_shutdown(self.hardware_failure)

    def _clock32_to_time(self, clock):
        clock64 = self._mcu.clock32_to_clock64(clock)
        return self._mcu.clock_to_print_time(clock64)

    def _start_streaming(self):
        if self._stream_en == 0 and self.beacon_stream_cmd is not None:
            self.beacon_stream_cmd.send([1])
            curtime = self.reactor.monotonic()
            self.reactor.update_timer(
                self._stream_timeout_timer, curtime + STREAM_TIMEOUT
            )
        self._stream_en += 1
        self._data_filter.reset()
        self._stream_flush()

    def _stop_streaming(self):
        self._stream_en -= 1
        if self._stream_en == 0:
            self.reactor.update_timer(self._stream_timeout_timer, self.reactor.NEVER)
            if self.beacon_stream_cmd is not None:
                self.beacon_stream_cmd.send([0])
        self._stream_flush()

    def force_stop_streaming(self):
        self.reactor.update_timer(self._stream_timeout_timer, self.reactor.NEVER)
        if self.beacon_stream_cmd is not None:
            self.beacon_stream_cmd.send([0])
        self._stream_flush()

    def _stream_timeout(self, eventtime):
        if self._stream_flush():
            return eventtime + STREAM_TIMEOUT
        if not self._stream_en:
            return self.reactor.NEVER
        if not self.printer.is_shutdown():
            msg = "Beacon sensor not receiving data"
            logging.error(msg)
            self.printer.invoke_shutdown(msg)
        return self.reactor.NEVER

    def request_stream_latency(self, latency):
        next_key = 0
        if self._stream_latency_requests:
            next_key = max(self._stream_latency_requests.keys()) + 1
        new_limit = STREAM_BUFFER_LIMIT_DEFAULT
        self._stream_latency_requests[next_key] = latency
        min_requested = min(self._stream_latency_requests.values())
        if min_requested < new_limit:
            new_limit = min_requested
        if new_limit < 1:
            new_limit = 1
        self._stream_buffer_limit_new = new_limit
        return next_key

    def drop_stream_latency_request(self, key):
        self._stream_latency_requests.pop(key, None)
        new_limit = STREAM_BUFFER_LIMIT_DEFAULT
        if self._stream_latency_requests:
            min_requested = min(self._stream_latency_requests.values())
            if min_requested < new_limit:
                new_limit = min_requested
        if new_limit < 1:
            new_limit = 1
        self._stream_buffer_limit_new = new_limit

    def streaming_session(self, callback, completion_callback=None, latency=None):
        return StreamingHelper(self, callback, completion_callback, latency)

    def _stream_flush_message(self, msg):
        last = None
        for sample in msg:
            (clock, data) = sample
            temp = self.last_temp
            if self.model_temp is not None and not (-40 < temp < 180):
                msg = (
                    "Beacon temperature sensor faulty(read %.2f C),"
                    " disabling temperature compensation" % (temp,)
                )
                logging.error(msg)
                self.gcode.respond_raw("!! " + msg + "\n")
                self.model_temp = None
            if temp:
                self.measured_min = min(self.measured_min, temp)
                self.measured_max = max(self.measured_max, temp)

            clock = self._mcu.clock32_to_clock64(clock)
            time = self._mcu.clock_to_print_time(clock)
            self._data_filter.update(time, data)
            data_smooth = self._data_filter.value()
            freq = self.count_to_freq(data_smooth)
            dist = self.freq_to_dist(freq, temp)
            pos = self._get_position_at_time(time)
            if pos is not None:
                if dist is not None:
                    dist -= self.get_z_compensation_value(pos)
            last = sample = {
                "temp": temp,
                "clock": clock,
                "time": time,
                "data": data,
                "data_smooth": data_smooth,
                "freq": freq,
                "dist": dist,
            }
            if pos is not None:
                sample["pos"] = pos
            self._check_hardware(sample)

            if len(self._stream_callbacks) > 0:
                for cb in list(self._stream_callbacks.values()):
                    cb(sample)
        if last is not None:
            last = last.copy()
            dist = last["dist"]
            if dist is None or np.isinf(dist) or np.isnan(dist):
                del last["dist"]
            self.last_received_sample = last

    def _stream_flush(self):
        self._stream_flush_event.clear()
        updated_timer = False
        while True:
            try:
                samples = self._stream_samples_queue.get_nowait()
                updated_timer = False
                for sample in samples:
                    if not updated_timer:
                        curtime = self.reactor.monotonic()
                        self.reactor.update_timer(
                            self._stream_timeout_timer, curtime + STREAM_TIMEOUT
                        )
                        updated_timer = True
                    self._stream_flush_message(sample)
            except queue.Empty:
                return updated_timer

    def _stream_flush_schedule(self):
        force = self._stream_en == 0  # When streaming is disabled, let all through
        if self._stream_buffer_limit_new != self._stream_buffer_limit:
            force = True
            self._stream_buffer_limit = self._stream_buffer_limit_new
        if not force and self._stream_buffer_count < self._stream_buffer_limit:
            return
        self._stream_samples_queue.put_nowait(self._stream_buffer)
        self._stream_buffer = []
        self._stream_buffer_count = 0
        if self._stream_flush_event.is_set():
            return
        self._stream_flush_event.set()
        self.reactor.register_async_callback(lambda e: self._stream_flush())

    def _handle_beacon_data(self, params):
        if self.trapq is None:
            return

        buf = bytearray(params["data"])
        sample_count = params["samples"]
        start_clock = params["start_clock"]
        delta_clock = (
            params["delta_clock"] / (sample_count - 1) if sample_count > 1 else 0
        )

        samples = []
        data = 0
        for i in range(0, sample_count):
            if buf[0] & 0x80 == 0:
                delta = ((buf[0] & 0x7F) << 8) + buf[1]
                data = data + delta - ((buf[0] & 0x40) << 9)
                buf = buf[2:]
            else:
                data = (buf[0] & 0x7F) << 24 | buf[1] << 16 | buf[2] << 8 | buf[3]
                buf = buf[4:]
            clock = start_clock + int(round(i * delta_clock))
            samples.append((clock, data))

        self._stream_buffer.append(samples)
        self._stream_buffer_count += len(samples)
        self._stream_flush_schedule()

    def _get_position_at_time(self, print_time):
        kin = self.kinematics
        pos = {
            s.get_name(): s.mcu_to_commanded_position(
                s.get_past_mcu_position(print_time)
            )
            for s in kin.get_steppers()
        }
        return kin.calc_position(pos)

    def _sample_printtime_sync(self, skip=0, count=1):
        move_time = self.toolhead.get_last_move_time()
        settle_clock = self._mcu.print_time_to_clock(move_time)
        samples = []
        total = skip + count

        def cb(sample):
            if sample["clock"] >= settle_clock:
                samples.append(sample)
                if len(samples) >= total:
                    raise StopStreaming

        with self.streaming_session(cb, latency=skip + count) as ss:
            ss.wait()

        samples = samples[skip:]

        if count == 1:
            return samples[0]
        else:
            return samples

    def _sample(self, skip, count):
        samples = self._sample_printtime_sync(skip, count)
        return (median([s["dist"] for s in samples]), samples)

    def _sample_async(self, count=1):
        samples = []

        def cb(sample):
            samples.append(sample)
            if len(samples) >= count:
                raise StopStreaming

        with self.streaming_session(cb, latency=count) as ss:
            ss.wait()

        if count == 1:
            return samples[0]
        else:
            return samples

    def count_to_freq(self, count):
        return count * self._mcu_freq / (2**28)

    def freq_to_count(self, freq):
        return freq * (2**28) / self._mcu_freq

    def dist_to_freq(self, dist, temp):
        if self.model is None:
            return None
        return self.model.dist_to_freq(dist, temp)

    def freq_to_dist(self, freq, temp):
        if self.model is None:
            return None
        return self.model.freq_to_dist(freq, temp)

    def get_status(self, eventtime):
        model = None
        if self.model is not None:
            model = self.model.name
        return {
            "last_sample": self.last_sample,
            "last_received_sample": self.last_received_sample,
            "last_z_result": self.last_z_result,
            "last_probe_position": self.last_probe_position,
            "last_probe_result": self.last_probe_result,
            "last_offset_result": self.last_offset_result,
            "last_poke_result": self.last_poke_result,
            "model": model,
        }

    # Webhook handlers

    def _handle_req_status(self, web_request):
        temp = None
        sample = self._sample_async()
        out = {
            "freq": sample["freq"],
            "dist": sample["dist"],
        }
        temp = sample["temp"]
        if temp is not None:
            out["temp"] = temp
        web_request.send(out)

    def _handle_req_dump(self, web_request):
        self._api_dump.add_web_client(web_request)
        web_request.send({"header": API_DUMP_FIELDS})

    # Compat wrappers

    def compat_toolhead_set_position_homing_z(self, toolhead, pos):
        func = toolhead.set_position
        kind = tuple
        if hasattr(func, "__defaults__"):  # Python 3
            kind = type(func.__defaults__[0])
        else:  # Python 2
            kind = type(func.func_defaults[0])
        if kind is str:
            return toolhead.set_position(pos, homing_axes="z")
        else:
            return toolhead.set_position(pos, homing_axes=[2])

    def compat_kin_note_z_not_homed(self, kin):
        if hasattr(kin, "note_z_not_homed"):
            kin.note_z_not_homed()
        elif hasattr(kin, "clear_homing_state"):
            kin.clear_homing_state("z")

    def compat_serial_port(self, mcu):
        if hasattr(mcu, "_serialport"):
            return mcu._serialport
        elif hasattr(mcu, "_conn_helper"):
            return mcu._conn_helper.get_serialport()[0]
        else:
            raise Exception("Could not determine serial port")

    probe_result_builder = None

    def compat_create_probe_result(self, test_pos):
        if BeaconProbe.probe_result_builder is None:
            if hasattr(manual_probe, "ProbeResult"):

                BeaconProbe.probe_result_builder = prb_proberesult
            else:

                BeaconProbe.probe_result_builder = prb_identity
        return BeaconProbe.probe_result_builder(self, test_pos)

    # GCode command handlers

    cmd_PROBE_help = "Probe Z-height at current XY position"

    def cmd_PROBE(self, gcmd):
        self.last_probe_result = "failed"
        pos = self.run_probe(gcmd)
        gcmd.respond_info("Result is z=%.6f" % (pos[2],))
        offset = self.get_offsets()
        self.last_z_result = pos[2] - offset[2]
        self.last_probe_position = (pos[0] - offset[0], pos[1] - offset[1])
        self.last_probe_result = "ok"

    cmd_BEACON_CALIBRATE_help = "Calibrate beacon response curve"

    def cmd_BEACON_CALIBRATE(self, gcmd):
        self._start_calibration(gcmd)

    cmd_BEACON_ESTIMATE_BACKLASH_help = "Estimate Z axis backlash"

    def cmd_BEACON_ESTIMATE_BACKLASH(self, gcmd):
        # Get to correct Z height
        overrun = gcmd.get_float("OVERRUN", 1.0)
        speed = gcmd.get_float("PROBE_SPEED", self.speed, above=0.0)
        cur_z = self.toolhead.get_position()[2]
        self.toolhead.manual_move([None, None, cur_z + overrun], speed)
        self.run_probe(gcmd)

        lift_speed = self.get_lift_speed(gcmd)
        target = gcmd.get_float("Z", self.trigger_distance)

        num_samples = gcmd.get_int("SAMPLES", 20)
        wait = self.z_settling_time

        samples_up = []
        samples_down = []

        next_dir = -1

        try:
            self._start_streaming()

            (cur_dist, _samples) = self._sample(wait, 10)
            pos = self.toolhead.get_position()
            missing = target - cur_dist
            target = pos[2] + missing
            gcmd.respond_info("Target kinematic Z is %.3f" % (target,))

            if target - overrun < 0:
                raise gcmd.error("Target minus overrun must exceed 0mm")

            while len(samples_up) + len(samples_down) < num_samples:
                liftpos = [None, None, target + overrun * next_dir]
                self.toolhead.manual_move(liftpos, lift_speed)
                liftpos = [None, None, target]
                self.toolhead.manual_move(liftpos, lift_speed)
                self.toolhead.wait_moves()
                (dist, _samples) = self._sample(wait, 10)
                {-1: samples_up, 1: samples_down}[next_dir].append(dist)
                next_dir = next_dir * -1

        finally:
            self._stop_streaming()

        res_up = median(samples_up)
        res_down = median(samples_down)

        gcmd.respond_info(
            "Median distance moving up %.5f, down %.5f, "
            "delta %.5f over %d samples"
            % (res_up, res_down, res_down - res_up, num_samples)
        )

    cmd_BEACON_QUERY_help = "Take a sample from the sensor"

    def cmd_BEACON_QUERY(self, gcmd):
        sample = self._sample_async()
        last_value = sample["freq"]
        dist = sample["dist"]
        temp = sample["temp"]
        self.last_sample = {
            "time": sample["time"],
            "value": last_value,
            "temp": temp,
            "dist": None if dist is None or np.isinf(dist) or np.isnan(dist) else dist,
        }
        if dist is None:
            gcmd.respond_info(
                "Last reading: %.2fHz, %.2fC, no model"
                % (
                    last_value,
                    temp,
                )
            )
        else:
            gcmd.respond_info(
                "Last reading: %.2fHz, %.2fC, %.5fmm" % (last_value, temp, dist)
            )

    cmd_BEACON_STREAM_help = "Enable Beacon Streaming"

    def cmd_BEACON_STREAM(self, gcmd):
        if self._log_stream is not None:
            self._log_stream.stop()
            self._log_stream = None
            gcmd.respond_info("Beacon Streaming disabled")
        else:
            f = None
            completion_cb = None
            fn = gcmd.get("FILENAME")
            f = open(fn, "w")

            def close_file():
                f.close()

            completion_cb = close_file
            f.write("time,data,data_smooth,freq,dist,temp,pos_x,pos_y,pos_z\n")

            def cb(sample):
                pos = sample.get("pos", None)
                obj = "%.4f,%d,%.2f,%.5f,%.5f,%.2f,%s,%s,%s\n" % (
                    sample["time"],
                    sample["data"],
                    sample["data_smooth"],
                    sample["freq"],
                    sample["dist"],
                    sample["temp"],
                    "%.3f" % (pos[0],) if pos is not None else "",
                    "%.3f" % (pos[1],) if pos is not None else "",
                    "%.3f" % (pos[2],) if pos is not None else "",
                )
                f.write(obj)

            self._log_stream = self.streaming_session(cb, completion_cb)
            gcmd.respond_info("Beacon Streaming enabled")

    cmd_PROBE_ACCURACY_help = "Probe Z-height accuracy at current XY position"

    def cmd_PROBE_ACCURACY(self, gcmd):
        speed = gcmd.get_float("PROBE_SPEED", self.speed, above=0.0)
        lift_speed = self.get_lift_speed(gcmd)
        sample_count = gcmd.get_int("SAMPLES", 10, minval=1)
        sample_retract_dist = gcmd.get_float("SAMPLE_RETRACT_DIST", 0)
        allow_faulty = gcmd.get_int("ALLOW_FAULTY_COORDINATE", 0) != 0
        pos = self.toolhead.get_position()
        gcmd.respond_info(
            "PROBE_ACCURACY at X:%.3f Y:%.3f Z:%.3f"
            " (samples=%d retract=%.3f"
            " speed=%.1f lift_speed=%.1f)\n"
            % (
                pos[0],
                pos[1],
                pos[2],
                sample_count,
                sample_retract_dist,
                speed,
                lift_speed,
            )
        )

        start_height = self.trigger_distance + sample_retract_dist
        liftpos = [None, None, start_height]
        self.toolhead.manual_move(liftpos, lift_speed)

        self.multi_probe_begin()
        positions = []
        while len(positions) < sample_count:
            pos = self._probe(speed, allow_faulty=allow_faulty)
            positions.append(pos)
            self.toolhead.manual_move(liftpos, lift_speed)
        self.multi_probe_end()

        zs = [p[2] for p in positions]
        max_value = max(zs)
        min_value = min(zs)
        range_value = max_value - min_value
        avg_value = sum(zs) / len(positions)
        median_ = median(zs)

        deviation_sum = 0
        for i in range(len(zs)):
            deviation_sum += pow(zs[2] - avg_value, 2.0)
        sigma = (deviation_sum / len(zs)) ** 0.5

        gcmd.respond_info(
            "probe accuracy results: maximum %.6f, minimum %.6f, range %.6f, "
            "average %.6f, median %.6f, standard deviation %.6f"
            % (max_value, min_value, range_value, avg_value, median_, sigma)
        )

    cmd_Z_OFFSET_APPLY_PROBE_help = "Adjust the probe's z_offset"

    def cmd_Z_OFFSET_APPLY_PROBE(self, gcmd):
        gcode_move = self.printer.lookup_object("gcode_move")
        offset = gcode_move.get_status()["homing_origin"].z

        if offset == 0:
            self.gcode.respond_info("Nothing to do: Z Offset is 0")
            return

        if not self.model:
            raise self.gcode.error(
                "You must calibrate your model first, use BEACON_CALIBRATE."
            )

        # We use the model code to save the new offset, but we can't actually
        # apply that offset yet because the gcode_offset is still in effect.
        # If the user continues to do stuff after this, the newly set model
        # offset would compound with the gcode offset. To ensure this doesn't
        # happen, we revert to the old model offset afterwards.
        # Really, the user should just be calling `SAVE_CONFIG` now.
        old_offset = self.model.offset
        self.model.offset += offset
        self.model.save(self, False)
        gcmd.respond_info(
            "Beacon model offset has been updated, new value is %.5f\n"
            "You must run the SAVE_CONFIG command now to update the\n"
            "printer config file and restart the printer." % (self.model.offset,)
        )
        self.model.offset = old_offset

    cmd_BEACON_POKE_help = "Poke the bed"

    def cmd_BEACON_POKE(self, gcmd):
        top = gcmd.get_float("TOP", 5)
        bottom = gcmd.get_float("BOTTOM", -0.3)
        speed = gcmd.get_float("SPEED", 3, maxval=self.autocal_max_speed)

        pos = self.toolhead.get_position()
        gcmd.respond_info(
            "Poke test at (%.3f,%.3f), from %.3f to %.3f, at %.3f mm/s"
            % (pos[0], pos[1], top, bottom, speed)
        )

        self.last_probe_result = "failed"
        self.toolhead.manual_move([None, None, top], 100.0)
        self.toolhead.wait_moves()
        self.toolhead.dwell(0.5)

        ts = time.strftime("%Y%m%d_%H%M%S")
        fn = "/tmp/poke_%s_%.3f_%.3f-%.3f.csv" % (ts, speed, top, bottom)
        with open(fn, "w") as f:
            f.write("time,data,data_smooth,freq,dist,temp,pos_x,pos_y,pos_z\n")

            def cb(sample):
                pos = sample.get("pos", None)
                obj = "%.6f,%d,%.2f,%.5f,%.5f,%.2f,%s,%s,%s\n" % (
                    sample["time"],
                    sample["data"],
                    sample["data_smooth"],
                    sample["freq"],
                    sample["dist"],
                    sample["temp"],
                    "%.3f" % (pos[0],) if pos is not None else "",
                    "%.3f" % (pos[1],) if pos is not None else "",
                    "%.5f" % (pos[2],) if pos is not None else "",
                )
                f.write(obj)

            with self.streaming_session(cb):
                self._sample_async()
                self.toolhead.get_last_move_time()
                pos = self.toolhead.get_position()
                self.mcu_contact_probe.activate_gcode.run_gcode_from_command()
                try:
                    hmove = HomingMove(
                        self.printer, [(self.mcu_contact_probe, "contact")]
                    )
                    pos[2] = bottom
                    epos = hmove.homing_move(pos, speed, probe_pos=True)[:3]
                    self.toolhead.wait_moves()
                    spos = self.toolhead.get_position()[:3]
                    armpos = self._get_position_at_time(
                        self._clock32_to_time(self.last_contact_msg["armed_clock"])
                    )
                    gcmd.respond_info("Armed at:     z=%.5f" % (armpos[2],))
                    gcmd.respond_info(
                        "Triggered at: z=%.5f with latency=%d"
                        % (epos[2], self.last_contact_msg["latency"])
                    )
                    gcmd.respond_info(
                        "Overshoot:    %.3f um" % ((epos[2] - spos[2]) * 1000.0,)
                    )
                    self.last_probe_result = "ok"
                    self.last_poke_result = {
                        "target_position": pos,
                        "arming_z": armpos[2],
                        "trigger_z": epos[2],
                        "stopped_z": spos[2],
                        "latency": self.last_contact_msg["latency"],
                        "error": self.last_contact_msg["error"],
                    }
                except self.printer.command_error:
                    if self.printer.is_shutdown():
                        raise self.printer.command_error(
                            "Homing failed due to printer shutdown"
                        )
                    raise
                finally:
                    self.mcu_contact_probe.deactivate_gcode.run_gcode_from_command()
                    self.toolhead.manual_move([None, None, top], 100.0)
                    self.toolhead.wait_moves()

    cmd_BEACON_AUTO_CALIBRATE_help = "Automatically calibrates the Beacon probe"

    def cmd_BEACON_AUTO_CALIBRATE(self, gcmd):
        speed = gcmd.get_float(
            "SPEED", self.autocal_speed, above=0, maxval=self.autocal_max_speed
        )
        # [CUSTOM] New parameter approach_speed
        approach_speed = gcmd.get_float(
            "APPROACH_SPEED", self.approach_speed, above=0)
        desired_accel = gcmd.get_float("ACCEL", self.autocal_accel, minval=1)
        retract_dist = gcmd.get_float("RETRACT", self.autocal_retract_dist, minval=1)
        retract_speed = gcmd.get_float(
            "RETRACT_SPEED", self.autocal_retract_speed, minval=1
        )
        sample_count = gcmd.get_int("SAMPLES", self.autocal_sample_count, minval=1)
        tolerance = gcmd.get_float(
            "SAMPLES_TOLERANCE", self.autocal_tolerance, above=0.0
        )
        max_retries = gcmd.get_int(
            "SAMPLES_TOLERANCE_RETRIES", self.autocal_max_retries, minval=0
        )

        curtime = self.reactor.monotonic()
        kin = self.kinematics
        kin_status = kin.get_status(curtime)
        if "x" not in kin_status["homed_axes"] or "y" not in kin_status["homed_axes"]:
            raise gcmd.error("Must home X and Y axes first")

        self.last_probe_result = "failed"
        force_pos = self.toolhead.get_position()[:]
        home_pos = force_pos[:]
        amin, amax = kin_status["axis_minimum"][2], kin_status["axis_maximum"][2]
        force_pos[2] = amax
        home_pos[2] = amin

        stop_samples = []

        old_max_accel = self.toolhead.get_status(curtime)["max_accel"]
        gcode = self.printer.lookup_object("gcode")

        def set_max_accel(value):
            gcode.run_script_from_command("SET_VELOCITY_LIMIT ACCEL=%.3f" % (value,))

        homing_state = BeaconHomingState()
        self.printer.send_event("homing:home_rails_begin", homing_state, [])
        self.mcu_contact_probe.activate_gcode.run_gcode_from_command()
        try:
            self.compat_toolhead_set_position_homing_z(self.toolhead, force_pos)
            initial_approach = True
            retries = 0
            while len(stop_samples) < sample_count:
                # [CUSTOM] Logic for fast initial approach using proximity sensor
                if initial_approach:
                    gcmd.respond_info("Initial approach")
                    self.toolhead.wait_moves()
                    set_max_accel(desired_accel)
                    try:
                        self._probe(approach_speed, num_samples=2)
                    except self.printer.command_error:
                        if self.printer.is_shutdown():
                            raise self.printer.command_error(
                                "Homing failed due to printer shutdown"
                            )
                        raise
                    finally:
                        set_max_accel(old_max_accel)
                else:
                    gcmd.respond_info(
                        "Collecting sample %d/%d"
                        % (len(stop_samples) + 1, sample_count)
                    )
                self.toolhead.wait_moves()
                set_max_accel(desired_accel)
                try:
                    hmove = HomingMove(
                        self.printer, [(self.mcu_contact_probe, "contact")]
                    )
                    epos = hmove.homing_move(home_pos, speed, probe_pos=True)
                except self.printer.command_error:
                    if self.printer.is_shutdown():
                        raise self.printer.command_error(
                            "Homing failed due to printer shutdown"
                        )
                    raise
                finally:
                    set_max_accel(old_max_accel)

                retract_pos = self.toolhead.get_position()[:]
                retract_pos[2] += retract_dist
                if retract_pos[2] > amax:
                    retract_pos[2] = amax
                self.toolhead.move(retract_pos, retract_speed)
                self.toolhead.dwell(1.0)

                if not initial_approach:
                    stop_samples.append(epos[2])
                    mean = np.mean(stop_samples)
                    delta = max([abs(v - mean) for v in stop_samples])
                    if delta > tolerance:
                        if retries >= max_retries:
                            raise gcmd.error(
                                "Sample spread too large(%.4f > %.4f)"
                                % (delta, tolerance)
                            )
                        gcmd.respond_info(
                            "Sample spread too large(%.4f > %.4f), restarting"
                            % (delta, tolerance)
                        )
                        retries += 1
                        stop_samples = []
                        initial_approach = True
                else:
                    initial_approach = False

            gcmd.respond_info(
                "Collected %d samples, %.4f sd"
                % (len(stop_samples), np.std(stop_samples))
            )

            current_delta = force_pos[2] - self.toolhead.get_position()[2]
            true_zero_delta = force_pos[2] - np.mean(stop_samples)

            force_pos[2] = float(true_zero_delta - current_delta)
            self.toolhead.set_position(force_pos)

            self.toolhead.wait_moves()
            self.toolhead.flush_step_generation()
            self.last_probe_result = "ok"
            self.printer.send_event("homing:home_rails_end", homing_state, [])
            if gcmd.get_int("SKIP_MODEL_CREATION", 0) == 0:
                self._calibrate(gcmd, force_pos, force_pos[2], True, True)

        except self.printer.command_error:
            self.compat_kin_note_z_not_homed(kin)
            raise
        finally:
            self.mcu_contact_probe.deactivate_gcode.run_gcode_from_command()

    cmd_BEACON_OFFSET_COMPARE_help = (
        "Measures offset between contact and proximity measurements"
    )

    def cmd_BEACON_OFFSET_COMPARE(self, gcmd):
        top = gcmd.get_float("TOP", 2)

        self.last_probe_result = "failed"
        self.toolhead.get_last_move_time()
        self._sample_async()
        start_pos = self.toolhead.get_position()

        params = {
            "SAMPLES_DROP": 1,
            "SAMPLES": 3,
        }
        params.update(gcmd.get_command_parameters())

        # Do contact move
        epos = self._run_probe_contact(
            self.gcode.create_gcode_command(
                "PROBE",
                "PROBE",
                params,
            )
        )

        # Up
        self.toolhead.manual_move([None, None, top + 0.5], 100.0)

        # Over
        pos = start_pos[:2]
        pos[0] -= self.x_offset
        pos[1] -= self.y_offset
        self.toolhead.manual_move(pos, 100.0)
        self.toolhead.wait_moves()

        # Down
        self.toolhead.manual_move([None, None, 2.0], 100.0)

        # Query
        (dist, _samples) = self._sample(self.z_settling_time, 10)
        dist = 2.0 - dist

        # Back
        self.toolhead.manual_move(start_pos, 100.0)
        self.toolhead.wait_moves()

        delta = epos[2] - dist
        gcmd.respond_info("Comparing @ %.4f,%.4f" % (start_pos[0], start_pos[1]))
        gcmd.respond_info("Contact:   %.5f mm" % (epos[2],))
        gcmd.respond_info("Proximity: %.5f mm" % (dist,))
        gcmd.respond_info("Delta:     %.3f um" % (delta * 1000,))
        self.last_probe_result = "ok"
        self.last_offset_result = {
            "position": (start_pos[0], start_pos[1], epos[2]),
            "delta": delta,
        }


class BeaconModel:
    @classmethod
    def load(cls, name, config, beacon):
        coef = config.getfloatlist("model_coef")
        temp = config.getfloat("model_temp")
        domain = config.getfloatlist("model_domain", count=2)
        [min_z, max_z] = config.getfloatlist("model_range", count=2)
        offset = config.getfloat("model_offset", 0.0)
        poly = Polynomial(coef, domain)
        return BeaconModel(name, beacon, poly, temp, min_z, max_z, offset)

    def __init__(self, name, beacon, poly, temp, min_z, max_z, offset=0):
        self.name = name
        self.beacon = beacon
        self.poly = poly
        self.min_z = min_z
        self.max_z = max_z
        self.temp = temp
        self.offset = offset

    def save(self, beacon, show_message=True):
        configfile = beacon.printer.lookup_object("configfile")
        sensor_name = "" if beacon.id.is_unnamed() else "sensor %s " % (beacon.id.name)
        section = "beacon " + sensor_name + "model " + self.name
        configfile.set(section, "model_coef", ",\n ".join(map(str, self.poly.coef)))
        configfile.set(section, "model_domain", ",".join(map(str, self.poly.domain)))
        configfile.set(section, "model_range", "%f,%f" % (self.min_z, self.max_z))
        configfile.set(section, "model_temp", "%f" % (self.temp))
        configfile.set(section, "model_offset", "%.5f" % (self.offset,))
        if show_message:
            beacon.gcode.respond_info(
                "Beacon calibration for model '%s' has "
                "been updated\nfor the current session. The SAVE_CONFIG "
                "command will\nupdate the printer config file and restart "
                "the printer." % (self.name,)
            )

    def freq_to_dist_raw(self, freq):
        [begin, end] = self.poly.domain
        invfreq = 1 / freq
        if invfreq > end:
            return float("inf")
        elif invfreq < begin:
            return float("-inf")
        else:
            return float(self.poly(invfreq) - self.offset)

    def freq_to_dist(self, freq, temp):
        if self.temp is not None and self.beacon.model_temp is not None:
            freq = self.beacon.model_temp.compensate(freq, temp, self.temp)
        return self.freq_to_dist_raw(freq)

    def dist_to_freq_raw(self, dist, max_e=0.00000001):
        if dist < self.min_z or dist > self.max_z:
            msg = (
                "Attempted to map out-of-range distance %f, valid range "
                "[%.3f, %.3f]" % (dist, self.min_z, self.max_z)
            )
            raise self.beacon.printer.command_error(msg)
        dist += self.offset
        [begin, end] = self.poly.domain
        for _ in range(0, 50):
            f = (end + begin) / 2
            v = self.poly(f)
            if abs(v - dist) < max_e:
                return float(1.0 / f)
            elif v < dist:
                begin = f
            else:
                end = f
        raise self.beacon.printer.command_error("Beacon model convergence error")

    def dist_to_freq(self, dist, temp, max_e=0.00000001):
        freq = self.dist_to_freq_raw(dist, max_e)
        if self.temp is not None and self.beacon.model_temp is not None:
            freq = self.beacon.model_temp.compensate(freq, self.temp, temp)
        return freq


class BeaconMCUTempHelper:
    def __init__(self, temp_room, temp_hot, ref_room, ref_hot, adc_room, adc_hot):
        self.temp_room = temp_room
        self.temp_hot = temp_hot
        self.ref_room = ref_room
        self.ref_hot = ref_hot
        self.adc_room = adc_room
        self.adc_hot = adc_hot

    def compensate(self, beacon, mcu_temp, supply):
        temp_mcu_uncomp = self.temp_room + (self.temp_hot - self.temp_room) * (
            mcu_temp / beacon.temp_smooth_count - self.adc_room * self.ref_room
        ) / (self.adc_hot * self.ref_hot - self.adc_room * self.ref_room)
        ref_comp = self.ref_room + (self.ref_hot - self.ref_room) * (
            temp_mcu_uncomp - self.temp_room
        ) / (self.temp_hot - self.temp_room)
        temp_mcu_comp = self.temp_room + (self.temp_hot - self.temp_room) * (
            mcu_temp / beacon.temp_smooth_count * ref_comp
            - self.adc_room * self.ref_room
        ) / (self.adc_hot * self.ref_hot - self.adc_room * self.ref_room)
        supply_voltage = (
            4.0 * supply * ref_comp / beacon.temp_smooth_count * beacon.inv_adc_max
        )
        return (temp_mcu_comp, supply_voltage)

    @classmethod
    def build_with_nvm(cls, beacon):
        nvm_data = beacon.beacon_nvm_read_cmd.send([8, 65534])
        if nvm_data["offset"] == 65534:
            (lower, upper) = struct.unpack("<II", nvm_data["bytes"])
            temp_room = (lower & 0xFF) + 0.1 * ((lower >> 8) & 0xF)
            temp_hot = ((lower >> 12) & 0xFF) + 0.1 * ((lower >> 20) & 0xF)
            adc_room = (upper >> 8) & 0xFFF
            adc_hot = (upper >> 20) & 0xFFF
            (ref_room_raw, ref_hot_raw) = struct.unpack("<xxxbbxxx", nvm_data["bytes"])
            ref_room = 1.0 - ref_room_raw / 1000.0
            ref_hot = 1.0 - ref_hot_raw / 1000.0
            return cls(temp_room, temp_hot, ref_room, ref_hot, adc_room, adc_hot)
        else:
            return None


class BeaconTempModelBuilder:
    _DEFAULTS = {
        "amfg": 1.0,
        "tcc": -2.1429828e-05,
        "tcfl": -1.8980091e-10,
        "tctl": 3.6738370e-16,
        "fmin": None,
        "fmin_temp": None,
    }

    @classmethod
    def load(cls, config):
        return BeaconTempModelBuilder(config)

    def __init__(self, config):
        self.parameters = BeaconTempModelBuilder._DEFAULTS.copy()
        for key in self.parameters.keys():
            param = config.getfloat("tc_" + key, None)
            if param is not None:
                self.parameters[key] = param

    def build_with_nvm(self, beacon):
        nvm_data = beacon.beacon_nvm_read_cmd.send([20, 0])
        (ver,) = struct.unpack("<Bxxx", nvm_data["bytes"][12:16])
        if ver == 0x01:
            return BeaconTempModelV1.build_with_nvm(beacon, self.parameters, nvm_data)
        else:
            return BeaconTempModelV0.build_with_nvm(beacon, self.parameters, nvm_data)


class BeaconTempModelV0:
    def __init__(self, amfg, tcc, tcfl, tctl, fmin, fmin_temp):
        self.amfg = amfg
        self.tcc = tcc
        self.tcfl = tcfl
        self.tctl = tctl
        self.fmin = fmin
        self.fmin_temp = fmin_temp

    @classmethod
    def build_with_nvm(cls, beacon, parameters, nvm_data):
        (f_count, adc_count) = struct.unpack("<IH", nvm_data["bytes"][:6])
        if f_count < 0xFFFFFFFF and adc_count < 0xFFFF:
            if parameters["fmin"] is None:
                parameters["fmin"] = beacon.count_to_freq(f_count)
                logging.info("beacon: loaded fmin=%.2f from nvm", parameters["fmin"])
            if parameters["fmin_temp"] is None:
                temp_adc = (
                    float(adc_count) / beacon.temp_smooth_count * beacon.inv_adc_max
                )
                parameters["fmin_temp"] = beacon.thermistor.calc_temp(temp_adc)
                logging.info(
                    "beacon: loaded fmin_temp=%.2f from nvm", parameters["fmin_temp"]
                )
        else:
            logging.info("beacon: parameters not found in nvm")
        if parameters["fmin"] is None or parameters["fmin_temp"] is None:
            return None
        logging.info("beacon: built tempco model version 0 %s", parameters)
        return cls(**parameters)

    def _tcf(self, f, df, dt, tctl):
        tctl = self.tctl if tctl is None else tctl
        tc = self.tcc + self.tcfl * df + tctl * df * df
        return f + self.amfg * tc * dt * f

    def compensate(self, freq, temp_source, temp_target, tctl=None):
        dt = temp_target - temp_source
        dfmin = self.fmin * self.amfg * self.tcc * (temp_source - self.fmin_temp)
        df = freq - (self.fmin + dfmin)
        if dt < 0.0:
            f2 = self._tcf(freq, df, dt, tctl)
            dfmin2 = self.fmin * self.amfg * self.tcc * (temp_target - self.fmin_temp)
            df2 = f2 - (self.fmin + dfmin2)
            f3 = self._tcf(f2, df2, -dt, tctl)
            ferror = freq - f3
            freq = freq + ferror
            df = freq - (self.fmin + dfmin)
        return self._tcf(freq, df, dt, tctl)


class BeaconTempModelV1:
    def __init__(self, amfg, tcc, tcfl, tctl, fmin, fmin_temp):
        self.amfg = amfg
        self.tcc = tcc
        self.tcfl = tcfl
        self.tctl = tctl
        self.fmin = fmin
        self.fmin_temp = fmin_temp

    @classmethod
    def build_with_nvm(cls, beacon, parameters, nvm_data):
        (fnorm, temp, ver, cal) = struct.unpack("<dfBxxxf", nvm_data["bytes"])
        amfg = cls._amfg(cal)
        logging.info(
            "beacon: loaded fnorm=%.2f temp=%.2f amfg=%.3f from nvm", fnorm, temp, amfg
        )
        if parameters["amfg"] == 1.0:
            parameters["amfg"] = amfg
        amfg = parameters["amfg"]
        if parameters["fmin"] is None:
            parameters["fmin"] = fnorm
        if parameters["fmin_temp"] is None:
            parameters["fmin_temp"] = temp
        parameters["tcc"] = cls._tcc(amfg)
        parameters["tcfl"] = cls._tcfl(amfg)
        parameters["tctl"] = cls._tctl(amfg)
        logging.info("beacon: built tempco model version 1 %s", parameters)
        return cls(**parameters)

    @classmethod
    def _amfg(cls, cal):
        return -3 * cal + 6.08660841

    @classmethod
    def _tcc(cls, amfg):
        return -1.3145444333476082e-05 * amfg + 6.142916519010881e-06

    @classmethod
    def _tcfl(cls, amfg):
        return 0.00018478916784300965 * amfg - 0.0008211578277775643

    @classmethod
    def _tctl(cls, amfg):
        return -0.0006829761203506137 * amfg + 0.0026317792448821123

    def _tcf(self, df):
        return self.tcc + self.tcfl * df + self.tctl * df * df

    def compensate(self, freq, temp_source, temp_target):
        if self.amfg == 0.0:
            return freq
        fnorm = freq / self.fmin
        dtmin = temp_source - self.fmin_temp
        tc = self._tcf(fnorm - 1.0)
        for _ in range(0, 3):
            fsl = fnorm * (1 + tc * -dtmin)
            tc = self._tcf(fsl - 1.0)
        dt = temp_target - temp_source
        return freq * (1 + tc * dt)


class ModelManager:
    def __init__(self, beacon):
        self.beacon = beacon
        self.gcode = beacon.printer.lookup_object("gcode")
        beacon.id.register_command(
            "BEACON_MODEL_SELECT",
            self.cmd_BEACON_MODEL_SELECT,
            desc=self.cmd_BEACON_MODEL_SELECT_help,
        )
        beacon.id.register_command(
            "BEACON_MODEL_SAVE",
            self.cmd_BEACON_MODEL_SAVE,
            desc=self.cmd_BEACON_MODEL_SAVE_help,
        )
        beacon.id.register_command(
            "BEACON_MODEL_REMOVE",
            self.cmd_BEACON_MODEL_REMOVE,
            desc=self.cmd_BEACON_MODEL_REMOVE_help,
        )
        beacon.id.register_command(
            "BEACON_MODEL_LIST",
            self.cmd_BEACON_MODEL_LIST,
            desc=self.cmd_BEACON_MODEL_LIST_help,
        )

    cmd_BEACON_MODEL_SELECT_help = "Load named beacon model"

    def cmd_BEACON_MODEL_SELECT(self, gcmd):
        name = gcmd.get("NAME")
        model = self.beacon.models.get(name, None)
        if model is None:
            raise gcmd.error("Unknown model '%s'" % (name,))
        self.beacon.model = model
        gcmd.respond_info("Selected Beacon model '%s'" % (name,))

    cmd_BEACON_MODEL_SAVE_help = "Save current beacon model"

    def cmd_BEACON_MODEL_SAVE(self, gcmd):
        model = self.beacon.model
        if model is None:
            raise gcmd.error("No model currently selected")
        oldname = model.name
        name = gcmd.get("NAME", oldname)
        if name != oldname:
            model = copy.copy(model)
            model.name = name
        model.save(self.beacon)
        if name != oldname:
            self.beacon.models[name] = model

    cmd_BEACON_MODEL_REMOVE_help = "Remove saved beacon model"

    def cmd_BEACON_MODEL_REMOVE(self, gcmd):
        name = gcmd.get("NAME")
        model = self.beacon.models.get(name, None)
        if model is None:
            raise gcmd.error("Unknown model '%s'" % (name,))
        configfile = self.beacon.printer.lookup_object("configfile")
        section = "beacon model " + model.name
        configfile.remove_section(section)
        self.beacon.models.pop(name)
        gcmd.respond_info(
            "Model '%s' was removed for the current session.\n"
            "Run SAVE_CONFIG to update the printer configuration"
            "and restart Klipper." % (name,)
        )
        if self.beacon.model == model:
            self.beacon.model = None

    cmd_BEACON_MODEL_LIST_help = "Remove saved beacon model"

    def cmd_BEACON_MODEL_LIST(self, gcmd):
        if not self.beacon.models:
            gcmd.respond_info("No Beacon models loaded")
            return
        gcmd.respond_info("List of loaded Beacon models:")
        current_model = self.beacon.model
        for _name, model in sorted(self.beacon.models.items()):
            if model == current_model:
                gcmd.respond_info("- %s [active]" % (model.name,))
            else:
                gcmd.respond_info("- %s" % (model.name,))


class AlphaBetaFilter:
    def __init__(self, alpha, beta):
        self.alpha = alpha
        self.beta = beta
        self.reset()

    def reset(self):
        self.xl = None
        self.vl = 0
        self.tl = None

    def update(self, time, measurement):
        if self.xl is None:
            self.xl = measurement
            if self.tl is not None:
                dt = time - self.tl
            else:
                dt = 0
            self.tl = time
        xk = self.xl + self.vl * dt
        vk = self.vl
        rk = measurement - xk
        xk = xk + self.alpha * rk
        if dt > 0:
            vk = vk + self.beta / dt * rk
        self.xl = xk
        self.vl = vk
        return xk

    def value(self):
        return self.xl


class StreamingHelper:
    def __init__(self, beacon, callback, completion_callback, latency):
        self.beacon = beacon
        self.cb = callback
        self.completion_cb = completion_callback
        self.completion = self.beacon.reactor.completion()
        self.latency_key = None
        if latency is not None:
            self.latency_key = self.beacon.request_stream_latency(latency)
        self.beacon._stream_callbacks[self] = self._handle
        self.beacon._start_streaming()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def _handle(self, sample):
        try:
            self.cb(sample)
        except StopStreaming:
            self.completion.complete(())

    def stop(self):
        if self not in self.beacon._stream_callbacks:
            return
        del self.beacon._stream_callbacks[self]
        self.beacon._stop_streaming()
        if self.latency_key is not None:
            self.beacon.drop_stream_latency_request(self.latency_key)
        if self.completion_cb is not None:
            self.completion_cb()

    def wait(self):
        self.completion.wait()
        self.stop()


class StopStreaming(Exception):
    pass


def prb_proberesult(beacon, test_pos):
    (x, y, z) = beacon.get_offsets()
    return manual_probe.ProbeResult(
        test_pos[0] + x,
        test_pos[1] + y,
        test_pos[2] - z,
        test_pos[0],
        test_pos[1],
        test_pos[2],
    )


def prb_identity(beacon, test_pos):
    return test_pos


class BeaconProbeWrapper:
    def __init__(self, beacon):
        self.beacon = beacon
        self.results = None

    def multi_probe_begin(self):
        return self.beacon.multi_probe_begin()

    def multi_probe_end(self):
        return self.beacon.multi_probe_end()

    def get_offsets(self):
        return self.beacon.get_offsets()

    def get_lift_speed(self, gcmd=None):
        return self.beacon.get_lift_speed(gcmd)

    def run_probe(self, gcmd):
        result = self.beacon.run_probe(gcmd)
        if self.results is not None:
            self.results.append(result)
        return result

    def get_probe_params(self, gcmd=None):
        return {"lift_speed": self.beacon.get_lift_speed(gcmd)}

    def start_probe_session(self, gcmd):
        self.multi_probe_begin()
        self.results = []
        return self

    def end_probe_session(self):
        self.results = None
        self.multi_probe_end()

    def pull_probed_results(self):
        results = self.results
        if results is None:
            return []
        else:
            self.results = []
        return results

    def get_status(self, eventtime):
        return {"name": "beacon"}


class BeaconTempWrapper:
    def __init__(self, beacon):
        self.beacon = beacon

    def get_temp(self, eventtime):
        return self.beacon.last_temp, 0

    def get_status(self, eventtime):
        return {
            "temperature": round(self.beacon.last_temp, 2),
            "measured_min_temp": round(self.beacon.measured_min, 2),
            "measured_max_temp": round(self.beacon.measured_max, 2),
        }


TRSYNC_TIMEOUT = 0.025


class BeaconEndstopShared:
    def __init__(self, beacon):
        self.beacon = beacon
        ffi_main, ffi_lib = chelper.get_ffi()
        self._trdispatch = ffi_main.gc(ffi_lib.trdispatch_alloc(), ffi_lib.free)
        self._trsync = MCU_trsync(self.beacon._mcu, self._trdispatch)
        self._trsyncs = [self._trsync]
        beacon.printer.register_event_handler(
            "klippy:mcu_identify", self._handle_mcu_identify
        )

    def _handle_mcu_identify(self):
        self.toolhead = self.beacon.printer.lookup_object("toolhead")
        kin = self.toolhead.get_kinematics()
        for stepper in kin.get_steppers():
            if stepper.is_active_axis("z"):
                self.add_stepper(stepper)

    def add_stepper(self, stepper):
        trsyncs = {trsync.get_mcu(): trsync for trsync in self._trsyncs}
        stepper_mcu = stepper.get_mcu()
        trsync = trsyncs.get(stepper_mcu)
        if trsync is None:
            trsync = MCU_trsync(stepper_mcu, self._trdispatch)
            self._trsyncs.append(trsync)
        trsync.add_stepper(stepper)
        # Check for unsupported multi-mcu shared stepper rails, duplicated
        # from MCU_endstop
        sname = stepper.get_name()
        if sname.startswith("stepper_"):
            for ot in self._trsyncs:
                for s in ot.get_steppers():
                    if ot is not trsync and s.get_name().startswith(sname[:9]):
                        raise self.beacon.printer.config_error(
                            "Multi-mcu homing not supported on multi-mcu shared axis"
                        )

    def get_steppers(self):
        return [s for trsync in self._trsyncs for s in trsync.get_steppers()]

    def trsync_start(self, print_time):
        self._trigger_completion = self.beacon.reactor.completion()
        expire_timeout = TRSYNC_TIMEOUT
        for i, trsync in enumerate(self._trsyncs):
            try:
                trsync.start(print_time, self._trigger_completion, expire_timeout)
            except TypeError:
                offset = float(i) / len(self._trsyncs)
                trsync.start(
                    print_time, offset, self._trigger_completion, expire_timeout
                )
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.trdispatch_start(self._trdispatch, self._trsync.REASON_HOST_REQUEST)
        return self._trigger_completion

    def trsync_stop(self, home_end_time):
        self._trsync.set_home_end_time(home_end_time)
        if self.beacon._mcu.is_fileoutput():
            self._trigger_completion.complete(True)
        self._trigger_completion.wait()
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.trdispatch_stop(self._trdispatch)
        res = [trsync.stop() for trsync in self._trsyncs]
        if any([r == self._trsync.REASON_COMMS_TIMEOUT for r in res]):
            cmderr = self.beacon.printer.command_error
            raise cmderr("Communication timeout during homing")
        if res[0] != self._trsync.REASON_ENDSTOP_HIT:
            return 0.0
        return None


class BeaconEndstopWrapper:
    def __init__(self, beacon):
        self.beacon = beacon
        self._shared = beacon._endstop_shared
        printer = beacon.printer
        printer.register_event_handler(
            "homing:home_rails_begin", self._handle_home_rails_begin
        )
        printer.register_event_handler(
            "homing:home_rails_end", self._handle_home_rails_end
        )
        self.is_homing = False

    def _handle_home_rails_begin(self, homing_state, rails):
        self.is_homing = False

    def _handle_home_rails_end(self, homing_state, rails):
        if self.beacon.model is None:
            return
        if not self.is_homing:
            return
        if 2 not in homing_state.get_axes():
            return

        # After homing Z we perform a measurement and adjust the toolhead
        # kinematic position.
        (dist, samples) = self.beacon._sample(self.beacon.z_settling_time, 10)
        if math.isinf(dist):
            logging.error("Post-homing adjustment measured samples %s", samples)
            raise self.beacon.printer.command_error(
                "Toolhead stopped below model range"
            )
        homing_state.set_homed_position([None, None, dist])

    def get_mcu(self):
        return self.beacon._mcu

    def add_stepper(self, stepper):
        self._shared.add_stepper(stepper)

    def get_steppers(self):
        return self._shared.get_steppers()

    def home_start(
        self, print_time, sample_time, sample_count, rest_time, triggered=True
    ):
        if self.beacon.model is None:
            raise self.beacon.printer.command_error("No Beacon model loaded")
        self.is_homing = True
        self.beacon._apply_threshold()
        self.beacon._sample_async()
        self._shared.trsync_start(print_time)
        etrsync = self._shared._trsync
        self.beacon.beacon_home_cmd.send(
            [
                etrsync.get_oid(),
                etrsync.REASON_ENDSTOP_HIT,
                0,
            ]
        )
        return self._shared._trigger_completion

    def home_wait(self, home_end_time):
        ret = self._shared.trsync_stop(home_end_time)
        self.beacon.beacon_stop_home_cmd.send()
        if ret is not None:
            return ret
        return home_end_time

    def query_endstop(self, print_time):
        if self.beacon.model is None:
            return 1
        self.beacon._mcu.print_time_to_clock(print_time)
        sample = self.beacon._sample_async()
        if self.beacon.trigger_freq <= sample["freq"]:
            return 1
        else:
            return 0

    def get_position_endstop(self):
        return self.beacon.trigger_distance


class BeaconContactEndstopWrapper:
    def __init__(self, beacon, config):
        self.beacon = beacon
        self._shared = beacon._endstop_shared
        gcode_macro = beacon.printer.load_object(config, "gcode_macro")
        self.activate_gcode = gcode_macro.load_template(
            config, "contact_activate_gcode", ""
        )
        self.deactivate_gcode = gcode_macro.load_template(
            config, "contact_deactivate_gcode", ""
        )
        self.max_hotend_temp = config.getfloat("contact_max_hotend_temperature", 180.0)

    def get_mcu(self):
        return self.beacon._mcu

    def add_stepper(self, stepper):
        self._shared.add_stepper(stepper)

    def get_steppers(self):
        return self._shared.get_steppers()

    def home_start(
        self, print_time, sample_time, sample_count, rest_time, triggered=True
    ):
        extruder = self.beacon.toolhead.get_extruder()
        if extruder is not None:
            curtime = self.beacon.reactor.monotonic()
            cur_temp = extruder.get_heater().get_status(curtime)["temperature"]
            if cur_temp >= self.max_hotend_temp:
                raise self.beacon.printer.command_error(
                    "Current hotend temperature %.1f exceeds maximum allowed temperature %.1f"
                    % (cur_temp, self.max_hotend_temp)
                )
        self.is_homing = True
        self.beacon._sample_async()
        self._shared.trsync_start(print_time)
        etrsync = self._shared._trsync
        if self.beacon.beacon_contact_set_latency_min_cmd is not None:
            self.beacon.beacon_contact_set_latency_min_cmd.send(
                [self.beacon.contact_latency_min]
            )
        if self.beacon.beacon_contact_set_sensitivity_cmd is not None:
            self.beacon.beacon_contact_set_sensitivity_cmd.send(
                [self.beacon.contact_sensitivity]
            )
        self.beacon.beacon_contact_home_cmd.send(
            [
                etrsync.get_oid(),
                etrsync.REASON_ENDSTOP_HIT,
                0,
                0,
            ]
        )
        return self._shared._trigger_completion

    def home_wait(self, home_end_time):
        try:
            ret = self._shared.trsync_stop(home_end_time)
            if ret is not None:
                return ret
            if self.beacon._mcu.is_fileoutput():
                return home_end_time
            self.beacon.toolhead.wait_moves()
            deadline = self.beacon.reactor.monotonic() + 0.5
            while True:
                ret = self.beacon.beacon_contact_query_cmd.send([])
                if ret["triggered"] == 0:
                    now = self.beacon.reactor.monotonic()
                    if now >= deadline:
                        raise self.beacon.printer.command_error(
                            "Timeout getting contact time"
                        )
                    self.beacon.reactor.pause(now + 0.001)
                    continue
                time = self.beacon._clock32_to_time(ret["detect_clock"])
                ffi_main, ffi_lib = chelper.get_ffi()
                data = ffi_main.new("struct pull_move[1]")
                count = ffi_lib.trapq_extract_old(
                    self.beacon.trapq, data, 1, 0.0, time
                )
                if count != 1:
                    raise self.beacon.printer.command_error(
                        "Unexpected trapq count in contact home"
                    )
                return time
        finally:
            self.beacon.beacon_contact_stop_home_cmd.send()

    def query_endstop(self, print_time):
        return 0


def load_config(config):
    return get_beacons(config).get_or_add_sensor(None)


def load_config_prefix(config):
    beacons = get_beacons(config)
    sensor = None
    secname = config.get_name()
    parts = secname[7:].split()

    if len(parts) != 0 and parts[0] == "sensor":
        if len(parts) < 2:
            raise config.error("Missing Beacon sensor name")
        sensor = parts[1]
        parts = parts[2:]

    beacon = beacons.get_or_add_sensor(sensor)

    if len(parts) == 0:
        return beacon

    if parts[0] == "model":
        if len(parts) != 2:
            raise config.error("Missing Beacon model name in section '%s'" % (secname,))
        name = parts[1]
        model = BeaconModel.load(name, config, beacon)
        beacon._register_model(name, model)
        return model

    raise config.error("Unknown Beacon config section '%s'" % (secname,))


def get_beacons(config):
    printer = config.get_printer()
    beacons = printer.lookup_object("beacons", None)
    if beacons is None:
        beacons = BeaconTracker(config, printer)
        printer.add_object("beacons", beacons)
    return beacons


class BeaconTracker:
    def __init__(self, config, printer):
        self.printer = printer
        self.sensors = {}
        self.gcode_commands = {}
        self.endpoints = {}
        self.webhooks = printer.lookup_object("webhooks")

        self.gcode = printer.lookup_object("gcode")
        self.gcode.register_command(
            "BEACON_VARS", self.cmd_BEACON_VARS, desc=self.cmd_BEACON_VARS_help
        )

    def get_or_add_sensor(self, name):
        if name in self.sensors:
            return self.sensors[name]
        sensor_id = BeaconSensorId(name, self)
        self.sensors[name] = beacon = BeaconProbe(
            self.printer.lookup_object("configfile").get_config_wrapper(
                self.printer, "beacon"
            ),
            sensor_id,
        )
        return beacon

    def register_gcode_command(self, sensor_name, cmd, func, desc):
        if cmd not in self.gcode_commands:
            self.gcode_commands[cmd] = {}
            self.gcode.register_command(cmd, self._dispatch_gcode_command, desc=desc)
        self.gcode_commands[cmd][sensor_name] = func

    def _dispatch_gcode_command(self, gcmd):
        cmd = gcmd.get_command()
        handlers = self.gcode_commands[cmd]
        if len(handlers) == 1 and None in handlers:
            return handlers[None](gcmd)
        name = gcmd.get("SENSOR", None)
        if name is None:
            if None in handlers:
                return handlers[None](gcmd)
            # If there is only one sensor, default to it
            if len(self.sensors) == 1:
                return handlers[list(self.sensors.keys())[0]](gcmd)
            raise gcmd.error("Must specify SENSOR for command '%s'" % (cmd,))
        if name not in handlers:
            raise gcmd.error(
                "Sensor '%s' does not support command '%s'" % (name, cmd)
            )
        return handlers[name](gcmd)

    def register_endpoint(self, sensor_name, path, callback):
        if path not in self.endpoints:
            self.endpoints[path] = {}
            self.webhooks.register_endpoint(path, self._dispatch_endpoint)
        self.endpoints[path][sensor_name] = callback

    def _dispatch_endpoint(self, web_request):
        path = web_request.get_path()
        handlers = self.endpoints[path]
        if len(handlers) == 1 and None in handlers:
            return handlers[None](web_request)
        # TODO: how to support multiple sensors here?
        # Maybe argument in path or query string?
        # For now just grab the first one
        return handlers[list(handlers.keys())[0]](web_request)

    cmd_BEACON_VARS_help = "Get beacon internal variables"

    def cmd_BEACON_VARS(self, gcmd):
        name = gcmd.get("SENSOR", None)
        if name is None and len(self.sensors) == 1:
            name = list(self.sensors.keys())[0]
        if name not in self.sensors:
            raise gcmd.error("Must specify SENSOR")
        beacon = self.sensors[name]
        gcmd.respond_info("Beacon variables:")
        gcmd.respond_info("  approach_speed: %.2f" % (beacon.approach_speed,))
        gcmd.respond_info("  autocal_accel: %.2f" % (beacon.autocal_accel,))
        gcmd.respond_info("  autocal_speed: %.2f" % (beacon.autocal_speed,))
        gcmd.respond_info(
            "  autocal_retract_dist: %.2f" % (beacon.autocal_retract_dist,)
        )
        gcmd.respond_info(
            "  autocal_retract_speed: %.2f" % (beacon.autocal_retract_speed,)
        )
        gcmd.respond_info("  autocal_sample_count: %d" % (beacon.autocal_sample_count,))
        gcmd.respond_info("  autocal_tolerance: %.4f" % (beacon.autocal_tolerance,))
        gcmd.respond_info("  autocal_max_retries: %d" % (beacon.autocal_max_retries,))
        gcmd.respond_info("  z_settling_time: %d" % (beacon.z_settling_time,))
        gcmd.respond_info("  contact_latency_min: %d" % (beacon.contact_latency_min,))
        gcmd.respond_info("  contact_sensitivity: %d" % (beacon.contact_sensitivity,))
        gcmd.respond_info("  speed: %.2f" % (beacon.speed,))
        gcmd.respond_info("  lift_speed: %.2f" % (beacon.lift_speed,))
        gcmd.respond_info("  backlash_comp: %.2f" % (beacon.backlash_comp,))
        gcmd.respond_info("  x_offset: %.2f" % (beacon.x_offset,))
        gcmd.respond_info("  y_offset: %.2f" % (beacon.y_offset,))
        gcmd.respond_info("  trigger_distance: %.2f" % (beacon.trigger_distance,))
        gcmd.respond_info(
            "  trigger_dive_threshold: %.2f" % (beacon.trigger_dive_threshold,)
        )
        gcmd.respond_info("  trigger_hysteresis: %.4f" % (beacon.trigger_hysteresis,))
        gcmd.respond_info("  cal_nozzle_z: %.2f" % (beacon.cal_nozzle_z,))
        gcmd.respond_info("  cal_floor: %.2f" % (beacon.cal_floor,))
        gcmd.respond_info("  cal_ceil: %.2f" % (beacon.cal_ceil,))
        gcmd.respond_info("  cal_speed: %.2f" % (beacon.cal_speed,))
        gcmd.respond_info("  cal_move_speed: %.2f" % (beacon.cal_move_speed,))
        gcmd.respond_info("  default_probe_method: %s" % (beacon.default_probe_method,))
        gcmd.respond_info("  contact_max_hotend_temperature: %.1f" % (beacon.mcu_contact_probe.max_hotend_temp,))

    def home_dir(self):
        return os.path.dirname(os.path.realpath(__file__))


class APIDumpHelper:
    def __init__(self, printer, start_callback, stop_callback, log_callback):
        self.printer = printer
        self.start_callback = start_callback
        self.stop_callback = stop_callback
        self.log_callback = log_callback
        self.clients = []
        self.buffer = []
        self.stream = None
        self.lock = threading.Lock()

    def add_web_client(self, web_request):
        with self.lock:
            self.clients.append(web_request)
            if self.stream is None:
                self.stream = self.start_callback()
                self.printer.register_event_handler("klippy:disconnect", self._handle_disconnect)

        def disconnect():
            with self.lock:
                if web_request in self.clients:
                    self.clients.remove(web_request)
                    if not self.clients:
                        self.stop_callback(self.stream)
                        self.stream = None
                        self.buffer = []

        web_request.on_disconnect(disconnect)
        self.printer.get_reactor().register_async_callback(lambda e: self._send(web_request))

    def _handle_disconnect(self):
        with self.lock:
            self.clients = []
            if self.stream is not None:
                self.stop_callback(self.stream)
                self.stream = None
                self.buffer = []

    def _send(self, web_request):
        if web_request not in self.clients:
            return
        if not self.buffer:
            self.printer.get_reactor().register_timer(
                lambda e: self._send(web_request), self.printer.get_reactor().monotonic() + 0.1
            )
            return
        with self.lock:
            data = list(self.buffer)
            self.buffer = []
        web_request.send({"data": data})
        self.printer.get_reactor().register_async_callback(lambda e: self._send(web_request))


class BeaconAccelConfig:
    def __init__(self, config):
        self.cs_pin = config.get("accel_cs_pin", "beacon:CS")
        self.spi_speed = config.getint("accel_spi_speed", 5000000)
        self.axes_map = config.getlist("accel_axes_map", ("x", "y", "z"))


class BeaconAccelHelper:
    def __init__(self, beacon, config, constants):
        self.beacon = beacon
        self.printer = beacon.printer
        self.name = beacon.name
        self.config = config
        self.adxl = adxl345.ADXL345(config)
        self.adxl_client = None

    def reinit(self, constants):
        self.adxl = adxl345.ADXL345(self.config)

    def start_internal_client(self):
        self.adxl_client = self.adxl.start_internal_client()
        return self.adxl_client


class BeaconSensorId:
    def __init__(self, name, tracker):
        self.name = name
        self.tracker = tracker

    def is_unnamed(self):
        return self.name is None

    def register_command(self, cmd, func, desc):
        self.tracker.register_gcode_command(self.name, cmd, func, desc)

    def register_endpoint(self, path, callback):
        self.tracker.register_endpoint(self.name, path, callback)