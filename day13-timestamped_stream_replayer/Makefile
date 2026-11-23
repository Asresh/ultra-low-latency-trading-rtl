PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
SBY ?= sby
BUILD := build
TOP := timestamped_stream_replayer
RTL := $(shell cat rtl.f)
DEPTHS := 1 3 16 32
CASES := $(foreach depth,$(DEPTHS),$(BUILD)/depth-$(depth).verified)
TRAFFIC := dense sparse mixed
EXPERIMENTS := $(foreach traffic,$(TRAFFIC),$(BUILD)/$(traffic).verified)

.PHONY: all test lint synth formal demo clean
all: test

$(BUILD):
	mkdir -p $@

$(BUILD)/depth-%.vvp: tb_timestamped_stream_replayer.sv $(RTL) replay_codes.svh rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -s tb_timestamped_stream_replayer -Ptb_timestamped_stream_replayer.DEPTH=$* -o $@ -f rtl.f $<

$(BUILD)/depth-%.stim: replay_vectors.py replay_program.py replay_model.py | $(BUILD)
	$(PYTHON) -B replay_vectors.py $@ --depth $* --seed $$(expr 13073 + $*)

$(BUILD)/depth-%.verified: $(BUILD)/depth-%.vvp $(BUILD)/depth-%.stim replay_audit.py replay_metrics.py replay_cli.py replay_control.py replay_format.py replay_capture.py replay_planner.py
	$(VVP) $(BUILD)/depth-$*.vvp +stim=$(BUILD)/depth-$*.stim +trace=$(BUILD)/depth-$*.trace
	$(PYTHON) -B replay_audit.py --depth $* --directed $(BUILD)/depth-$*.stim $(BUILD)/depth-$*.trace
	$(PYTHON) -B replay_cli.py metrics $(BUILD)/depth-$*.stim $(BUILD)/depth-$*.trace > $(BUILD)/depth-$*.metrics.json
	touch $@

$(BUILD)/limits.vvp: tb_replayer_limits.sv $(RTL) replay_codes.svh rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -s tb_replayer_limits -o $@ -f rtl.f $<

$(BUILD)/%.verified: $(BUILD)/depth-32.vvp replay_experiment.py replay_compare.py replay_format.py replay_capture.py replay_planner.py replay_metrics.py replay_audit.py replay_model.py replay_program.py replay_vectors.py
	$(PYTHON) -B replay_experiment.py build $(BUILD)/$* --profile $*
	$(VVP) $(BUILD)/depth-32.vvp +stim=$(BUILD)/$*/input.stim +trace=$(BUILD)/$*/output.trace
	$(PYTHON) -B replay_experiment.py check $(BUILD)/$*
	touch $@

test: $(CASES) $(EXPERIMENTS) $(BUILD)/limits.vvp
	$(VVP) $(BUILD)/limits.vvp
	$(PYTHON) -B -m unittest discover

lint:
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -f rtl.f

synth: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/synth.log -m slang -p 'read_slang --top $(TOP) $(RTL); synth -top $(TOP) -noabc; check -assert; stat'
	tail -n 20 $(BUILD)/synth.log

formal: | $(BUILD)
	cd formal && $(SBY) -f --prefix ../$(BUILD)/formal replayer.sby

demo: $(BUILD)/depth-16.verified
	$(PYTHON) -B replay_cli.py metrics $(BUILD)/depth-16.stim $(BUILD)/depth-16.trace | head -60

clean:
	rm -rf $(BUILD)

.SECONDARY:
