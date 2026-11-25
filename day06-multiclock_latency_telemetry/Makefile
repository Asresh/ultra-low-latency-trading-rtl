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
DISCIPLINE_TOP := latency_disciplined_timebase
DISCIPLINE_RTL := $(shell cat discipline.f)
DISCIPLINE_TB := tb_latency_pps_discipline
DISCIPLINE_SIGNALS := tb_latency_disciplined_timebase
RTL := $(shell cat rtl.f)
TB := tb_latency_axil tb_latency_trace tb_latency_timebase tb_latency_probe_burst tb_latency_capacity tb_histogram_swap
COMPONENTS := $(addprefix $(BUILD)/,$(addsuffix .vvp,$(TB)))

.PHONY: waveform wave-vcd deadline-wave-vcd waveform-deadline all test test-deadline regress icarus lint synth synth-deadline clean
all: test
icarus: test
regress: test

test: test-deadline test-discipline $(COMPONENTS) $(BUILD)/correlator.vvp $(BUILD)/correlator-small.vvp $(BUILD)/histogram.vvp $(BUILD)/fifo.vvp $(BUILD)/fifo-reverse.vvp $(BUILD)/telemetry.vvp
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

lint: lint-discipline
	$(VERILATOR) --lint-only -Wall --top-module $(DEADLINE_TOP) $(DEADLINE_RTL)
	$(VERILATOR) --lint-only -Wall -Wno-SYNCASYNCNET --top-module $(TOP) -f rtl.f

synth: synth-deadline synth-discipline | $(BUILD)
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

waveform: wave-vcd waveform-deadline waveform-discipline
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

.PHONY: test-discipline lint-discipline synth-discipline waveform-discipline

$(BUILD)/discipline.vvp: $(DISCIPLINE_TB).sv $(DISCIPLINE_RTL) discipline.f | $(BUILD)
	$(IVERILOG) -g2012 -Wall -s $(DISCIPLINE_TB) -o $@ -f discipline.f $<

$(BUILD)/discipline-small.vvp: $(DISCIPLINE_TB).sv $(DISCIPLINE_RTL) discipline.f | $(BUILD)
	$(IVERILOG) -g2012 -Wall -s $(DISCIPLINE_TB) -P$(DISCIPLINE_TB).AUDIT_DEPTH=1 -o $@ -f discipline.f $<

$(BUILD)/discipline-default.vvp: $(DISCIPLINE_TB).sv $(DISCIPLINE_RTL) discipline.f | $(BUILD)
	$(IVERILOG) -g2012 -Wall -s $(DISCIPLINE_TB) -P$(DISCIPLINE_TB).REV_W=16 \
		-P$(DISCIPLINE_TB).AUDIT_DEPTH=8 -o $@ -f discipline.f $<

$(BUILD)/discipline-divider.vvp: tb_latency_period_divide.sv latency_period_divide.sv | $(BUILD)
	$(IVERILOG) -g2012 -Wall -s tb_latency_period_divide -o $@ $^

$(BUILD)/discipline-live.vvp: tb_latency_disciplined_timebase.sv $(DISCIPLINE_RTL) discipline.f | $(BUILD)
	$(IVERILOG) -g2012 -Wall -s tb_latency_disciplined_timebase -o $@ -f discipline.f $<

test-discipline: $(BUILD)/discipline.vvp $(BUILD)/discipline-small.vvp $(BUILD)/discipline-default.vvp $(BUILD)/discipline-divider.vvp $(BUILD)/discipline-live.vvp
	$(PYTHON) -B -m unittest test_pps_discipline
	$(PYTHON) -B gen_pps_discipline_vectors.py $(BUILD)/discipline.txt
	$(VVP) $< +vectors=$(BUILD)/discipline.txt
	$(PYTHON) -B gen_pps_discipline_vectors.py $(BUILD)/discipline-small.txt --depth 1 --seed 6218
	$(VVP) $(BUILD)/discipline-small.vvp +vectors=$(BUILD)/discipline-small.txt
	$(PYTHON) -B gen_pps_discipline_vectors.py $(BUILD)/discipline-default.txt --depth 8 --revision-width 16 --seed 59273
	$(VVP) $(BUILD)/discipline-default.vvp +vectors=$(BUILD)/discipline-default.txt
	$(PYTHON) -B gen_pps_divider_vectors.py $(BUILD)/discipline-divider.txt
	$(VVP) $(BUILD)/discipline-divider.vvp +vectors=$(BUILD)/discipline-divider.txt
	$(VVP) $(BUILD)/discipline-live.vvp +out=$(BUILD)
	$(PYTHON) -B verify_disciplined_capture.py $(BUILD)

lint-discipline:
	$(VERILATOR) --lint-only -Wall --top-module latency_pps_discipline -f discipline.f
	$(VERILATOR) --lint-only -Wall -Wno-SYNCASYNCNET --top-module $(DISCIPLINE_TOP) -f discipline.f

synth-discipline: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/discipline-synth.log -m slang -p 'read_slang --top $(DISCIPLINE_TOP) $(DISCIPLINE_RTL); synth -top $(DISCIPLINE_TOP) -noabc; check -assert; stat'
	tail -n 16 $(BUILD)/discipline-synth.log

waveform-discipline: $(BUILD)/discipline-live.vvp
	mkdir -p $(BUILD)/wave/discipline
	$(VVP) $< +out=$(BUILD)/wave/discipline +waves=$(BUILD)/wave/discipline/live.vcd
	$(PYTHON) -B verify_disciplined_capture.py $(BUILD)/wave/discipline
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/wave/discipline/live.vcd pps-discipline-waveform.svg \
		--start 3680 --end 5080 --title 'PPS capture, overrun, and first rate correction' \
		--signal '$(DISCIPLINE_SIGNALS).pps_async=Asynchronous Input' \
		--signal '$(DISCIPLINE_SIGNALS).pps_observed=Sample Accepted' \
		--signal '$(DISCIPLINE_SIGNALS).pps_missed=Capture Overruns' \
		--signal '$(DISCIPLINE_SIGNALS).mode=Servo Mode' \
		--signal '$(DISCIPLINE_SIGNALS).active_period=Applied 32.32 Period' \
		--signal '$(DISCIPLINE_SIGNALS).audit_valid=Audit Valid' \
		--signal '$(DISCIPLINE_SIGNALS).audit_code=Audit Code'
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/wave/discipline/live.vcd pps-holdover-waveform.svg \
		--start 11200 --end 21800 --title 'Missing PPS holds the rate through reacquisition' \
		--signal '$(DISCIPLINE_SIGNALS).pps_async=Asynchronous Input' \
		--signal '$(DISCIPLINE_SIGNALS).mode=Servo Mode' \
		--signal '$(DISCIPLINE_SIGNALS).anchor_valid=Trusted PPS Anchor' \
		--signal '$(DISCIPLINE_SIGNALS).holdovers=Holdover Entries' \
		--signal '$(DISCIPLINE_SIGNALS).active_period=Applied 32.32 Period'
