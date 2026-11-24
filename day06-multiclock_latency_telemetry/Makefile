PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
BUILD := build
TOP := multiclock_latency_telemetry
RTL := $(shell cat rtl.f)
TB := tb_latency_axil tb_latency_trace tb_latency_timebase tb_latency_probe_burst tb_latency_capacity tb_histogram_swap
COMPONENTS := $(addprefix $(BUILD)/,$(addsuffix .vvp,$(TB)))

.PHONY: waveform wave-vcd all test regress icarus lint synth clean
all: test
icarus: test
regress: test

test: $(COMPONENTS) $(BUILD)/correlator.vvp $(BUILD)/correlator-small.vvp $(BUILD)/histogram.vvp $(BUILD)/fifo.vvp $(BUILD)/fifo-reverse.vvp $(BUILD)/telemetry.vvp
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
	$(VERILATOR) --lint-only -Wall -Wno-SYNCASYNCNET --top-module $(TOP) -f rtl.f

synth: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/synth.log -m slang -p 'read_slang --top $(TOP) $(RTL); synth -top $(TOP) -noabc; check -assert; stat'
	tail -n 22 $(BUILD)/synth.log

wave-vcd: $(BUILD)/telemetry.vvp
	mkdir -p $(BUILD)/wave
	$(VVP) $< +out=$(BUILD)/wave +waves=$(BUILD)/wave/telemetry.vcd
	$(PYTHON) -B verify_latency_capture.py $(BUILD)/wave

waveform: wave-vcd
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

clean:
	rm -rf $(BUILD)
