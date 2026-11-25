PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
BUILD := build
TOP := multiclock_latency_telemetry
DEADLINE_TOP := latency_deadline_monitor
DEADLINE_RTL := latency_alert_fifo.sv latency_deadline_monitor.sv
DEADLINE_TB := tb_latency_deadline_monitor
RTL := $(shell cat rtl.f)
TB := tb_latency_axil tb_latency_trace tb_latency_timebase tb_latency_probe_burst tb_latency_capacity tb_histogram_swap
COMPONENTS := $(addprefix $(BUILD)/,$(addsuffix .vvp,$(TB)))

.PHONY: waveform wave-vcd deadline-wave-vcd waveform-deadline all test test-deadline regress icarus lint synth synth-deadline clean
all: test
icarus: test
regress: test

test: test-deadline $(COMPONENTS) $(BUILD)/correlator.vvp $(BUILD)/correlator-small.vvp $(BUILD)/histogram.vvp $(BUILD)/fifo.vvp $(BUILD)/fifo-reverse.vvp $(BUILD)/telemetry.vvp
	$(PYTHON) -B -m unittest test_latency_host test_latency_config test_latency_units
	$(PYTHON) gen_latency_vectors.py correlator $(BUILD)/correlator.txt
	$(VVP) $(BUILD)/correlator.vvp +vectors=$(BUILD)/correlator.txt
	$(PYTHON) gen_latency_vectors.py correlator $(BUILD)/correlator-small.txt --seed 27539 --index-width 2 --scan-log 2 --cycles 3200
	$(VVP) $(BUILD)/correlator-small.vvp +vectors=$(BUILD)/correlator-small.txt
	$(PYTHON) gen_latency_vectors.py histogram $(BUILD)/histogram.txt
	$(VVP) $(BUILD)/histogram.vvp +vectors=$(BUILD)/histogram.txt
	$(VVP) $(BUILD)/fifo.vvp
	$(VVP) $(BUILD)/fifo-reverse.vvp
	@set -e; for component in $(COMPONENTS); do $(VVP) "$$component"; done
	$(VVP) $(BUILD)/telemetry.vvp +out=$(BUILD)
	$(PYTHON) verify_latency_capture.py $(BUILD)
	$(PYTHON) -B verify_deadline_capture.py $(BUILD)

test-deadline: $(BUILD)/deadline.vvp $(BUILD)/deadline-single.vvp $(BUILD)/deadline-default.vvp
	$(PYTHON) -B -m unittest test_deadline
	$(PYTHON) -B gen_deadline_vectors.py $(BUILD)/deadline.txt
	$(VVP) $(BUILD)/deadline.vvp +vectors=$(BUILD)/deadline.txt +capture=$(BUILD)/deadline-differential-alerts.txt
	$(PYTHON) -B deadline_alert.py $(BUILD)/deadline-differential-alerts.txt > $(BUILD)/deadline-differential-alerts.jsonl
	$(PYTHON) -B gen_deadline_vectors.py $(BUILD)/deadline-single.txt --rules 1 --depth 1 --seed 8049
	$(VVP) $(BUILD)/deadline-single.vvp +vectors=$(BUILD)/deadline-single.txt
	$(PYTHON) -B gen_deadline_vectors.py $(BUILD)/deadline-default.txt --rules 4 --depth 8 --revision-width 16 --seed 90517
	$(VVP) $(BUILD)/deadline-default.vvp +vectors=$(BUILD)/deadline-default.txt

$(BUILD)/deadline.vvp: $(DEADLINE_TB).sv $(DEADLINE_RTL) | $(BUILD)
	$(IVERILOG) -g2012 -s $(DEADLINE_TB) -o $@ $^

$(BUILD)/deadline-single.vvp: $(DEADLINE_TB).sv $(DEADLINE_RTL) | $(BUILD)
	$(IVERILOG) -g2012 -s $(DEADLINE_TB) -P$(DEADLINE_TB).RULES=1 -P$(DEADLINE_TB).DEPTH=1 -o $@ $^

$(BUILD)/deadline-default.vvp: $(DEADLINE_TB).sv $(DEADLINE_RTL) | $(BUILD)
	$(IVERILOG) -g2012 -s $(DEADLINE_TB) -P$(DEADLINE_TB).RULES=4 -P$(DEADLINE_TB).DEPTH=8 -P$(DEADLINE_TB).REV_W=16 -o $@ $^

$(BUILD):
	mkdir -p $@

$(BUILD)/tb_%.vvp: tb_%.sv rtl.f $(RTL) | $(BUILD)
	$(IVERILOG) -g2012 -s tb_$* -o $@ -f rtl.f $<

$(BUILD)/correlator.vvp: tb_latency_correlator.sv rtl.f $(RTL) | $(BUILD)
	$(IVERILOG) -g2012 -s tb_latency_correlator -o $@ -f rtl.f $<

$(BUILD)/correlator-small.vvp: tb_latency_correlator.sv rtl.f $(RTL) | $(BUILD)
	$(IVERILOG) -g2012 -s tb_latency_correlator -Ptb_latency_correlator.INDEX_W=2 -Ptb_latency_correlator.SCAN_LOG=2 -o $@ -f rtl.f $<

$(BUILD)/histogram.vvp: tb_latency_histogram.sv rtl.f $(RTL) | $(BUILD)
	$(IVERILOG) -g2012 -s tb_latency_histogram -o $@ -f rtl.f $<

$(BUILD)/fifo.vvp: tb_latency_fifo.sv rtl.f $(RTL) | $(BUILD)
	$(IVERILOG) -g2012 -s tb_latency_fifo -o $@ -f rtl.f $<

$(BUILD)/fifo-reverse.vvp: tb_latency_fifo.sv rtl.f $(RTL) | $(BUILD)
	$(IVERILOG) -g2012 -s tb_latency_fifo -Ptb_latency_fifo.W_HALF=9 -Ptb_latency_fifo.R_HALF=3 -Ptb_latency_fifo.ADDRESS_W=1 -o $@ -f rtl.f $<

$(BUILD)/telemetry.vvp: tb_multiclock_latency_telemetry.sv rtl.f $(RTL) | $(BUILD)
	$(IVERILOG) -g2012 -s tb_multiclock_latency_telemetry -o $@ -f rtl.f $<

lint:
	$(VERILATOR) --lint-only -Wall --top-module $(DEADLINE_TOP) $(DEADLINE_RTL)
	$(VERILATOR) --lint-only -Wall -Wno-SYNCASYNCNET --top-module $(TOP) -f rtl.f

synth: synth-deadline | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/synth.log -m slang -p 'read_slang --top $(TOP) $(RTL); synth -top $(TOP) -noabc; check -assert; stat'
	tail -n 22 $(BUILD)/synth.log

synth-deadline: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/synth-deadline.log -m slang -p 'read_slang --top $(DEADLINE_TOP) $(DEADLINE_RTL); synth -top $(DEADLINE_TOP) -noabc; check -assert; stat'
	tail -n 20 $(BUILD)/synth-deadline.log

wave-vcd: $(BUILD)/telemetry.vvp
	mkdir -p $(BUILD)/wave
	$(VVP) $< +out=$(BUILD)/wave +waves=$(BUILD)/wave/telemetry.vcd
	$(PYTHON) -B verify_latency_capture.py $(BUILD)/wave
	$(PYTHON) -B verify_deadline_capture.py $(BUILD)/wave

waveform: wave-vcd waveform-deadline
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/wave/telemetry.vcd waveform.svg \
		--start 1535 --end 1720 --title 'Cross-clock tag pairing and duplicate start' \
		--signal 'tb_multiclock_latency_telemetry.ref_clk=Reference Clock' \
		--signal 'tb_multiclock_latency_telemetry.probe_valid=Probe Valid Mask' \
		--signal 'tb_multiclock_latency_telemetry.probe_end=Probe End Mask' \
		--signal 'tb_multiclock_latency_telemetry.dut.report_tag=Report Tag' \
		--signal 'tb_multiclock_latency_telemetry.dut.report_status=Report Status' \
		--signal 'tb_multiclock_latency_telemetry.dut.report_latency=Latency (ns)' \
		--signal 'tb_multiclock_latency_telemetry.event_valid=Report Valid' \
		--signal 'tb_multiclock_latency_telemetry.report_count=Reports Accepted'

deadline-wave-vcd: $(BUILD)/deadline.vvp
	mkdir -p $(BUILD)/wave
	$(PYTHON) -B gen_deadline_vectors.py $(BUILD)/wave/deadline-directed.txt --cycles 0
	$(VVP) $< +vectors=$(BUILD)/wave/deadline-directed.txt +deadline_waves=$(BUILD)/wave/deadline.vcd

waveform-deadline: deadline-wave-vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/wave/deadline.vcd deadline-waveform.svg \
		--start 180 --end 310 --title 'Deadline alerts hold through backpressure' \
		--signal 'tb_latency_deadline_monitor.clk=Reference Clock' \
		--signal 'tb_latency_deadline_monitor.sample_valid=Accepted Report' \
		--signal 'tb_latency_deadline_monitor.duration=Duration in Ticks' \
		--signal 'tb_latency_deadline_monitor.dut.transition=Alarm Transition' \
		--signal 'tb_latency_deadline_monitor.alert_valid=Alert Valid' \
		--signal 'tb_latency_deadline_monitor.alert_ready=Alert Ready' \
		--signal 'tb_latency_deadline_monitor.alert_code=Held Alert Code' \
		--signal 'tb_latency_deadline_monitor.alert_occupancy=Queued Alerts' \
		--signal 'tb_latency_deadline_monitor.lost_alerts=Lost Alerts'

clean:
	rm -rf $(BUILD)
