PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
SBY ?= sby
BUILD := build
TOP := timestamped_stream_replayer
BANKED_TOP := banked_stream_replayer
BANKED_TB := tb_banked_stream_replayer
RTL := $(shell cat rtl.f)
IMPAIRMENT_TOP := replay_impairment
IMPAIRMENT_TB := tb_replay_impairment
IMPAIRMENT_RTL := $(shell cat impairment.f)
IMPAIRMENT_DEMO := $(BUILD)/impairment-demo
IMPAIRMENT_SCOPE := tb_banked_impairment_demo
DEPTHS := 1 3 16 32
CASES := $(foreach depth,$(DEPTHS),$(BUILD)/depth-$(depth).verified)
TRAFFIC := dense sparse mixed
EXPERIMENTS := $(foreach traffic,$(TRAFFIC),$(BUILD)/$(traffic).verified)

.PHONY: all test test-banked test-banked-bundle lint synth synth-banked formal formal-banked demo waveform waveform-banked clean
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

test: test-banked test-banked-bundle test-impairment $(CASES) $(EXPERIMENTS) $(BUILD)/limits.vvp
	$(VVP) $(BUILD)/limits.vvp
	$(PYTHON) -B -m unittest discover

$(BUILD)/banked.vvp: $(BANKED_TB).sv $(RTL) replay_codes.svh rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -s $(BANKED_TB) -o $@ -f rtl.f $<

$(BUILD)/banked-default.vvp: $(BANKED_TB).sv $(RTL) replay_codes.svh rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -s $(BANKED_TB) -P$(BANKED_TB).DEPTH=16 -P$(BANKED_TB).RUN_W=32 -o $@ -f rtl.f $<

test-banked: $(BUILD)/banked.vvp $(BUILD)/banked-default.vvp
	$(PYTHON) -B gen_banked_replay_vectors.py $(BUILD)/banked.vec
	$(VVP) $(BUILD)/banked.vvp +vectors=$(BUILD)/banked.vec
	$(PYTHON) -B gen_banked_replay_vectors.py $(BUILD)/banked-default.vec --depth 16 --run-width 32 --seed 78173
	$(VVP) $(BUILD)/banked-default.vvp +vectors=$(BUILD)/banked-default.vec

test-banked-bundle: $(BUILD)/banked.vvp
	$(PYTHON) -B replay_banked_bundle.py fixture $(BUILD)/banked-demo
	$(PYTHON) -B replay_banked_bundle.py build $(BUILD)/banked-demo $(BUILD)/banked-bundle.vec
	$(VVP) $< +vectors=$(BUILD)/banked-bundle.vec +capture=$(BUILD)/banked-accepted.csv +bundle
	$(PYTHON) -B replay_banked_bundle.py check $(BUILD)/banked-demo $(BUILD)/banked-accepted.csv

lint: lint-impairment
	$(VERILATOR) --lint-only -Wall --top-module $(BANKED_TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -f rtl.f

synth: synth-banked synth-impairment | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/synth.log -m slang -p 'read_slang --top $(TOP) $(RTL); synth -top $(TOP) -noabc; check -assert; stat'
	tail -n 20 $(BUILD)/synth.log

synth-banked: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/synth-banked.log -m slang -p 'read_slang --top $(BANKED_TOP) $(RTL); synth -top $(BANKED_TOP) -noabc; check -assert; stat'
	tail -n 20 $(BUILD)/synth-banked.log

formal-banked: | $(BUILD)
	cd formal && $(SBY) -f --prefix ../$(BUILD)/formal-banked banked_replayer.sby

formal: formal-banked formal-impairment | $(BUILD)
	cd formal && $(SBY) -f --prefix ../$(BUILD)/formal replayer.sby

demo: $(BUILD)/depth-16.verified
	$(PYTHON) -B replay_cli.py metrics $(BUILD)/depth-16.stim $(BUILD)/depth-16.trace | head -60

WAVE_SCOPE := tb_timestamped_stream_replayer
waveform: waveform-banked waveform-impairment $(BUILD)/depth-16.vvp $(BUILD)/depth-16.stim
	$(VVP) $(BUILD)/depth-16.vvp +stim=$(BUILD)/depth-16.stim +trace=$(BUILD)/waveform.trace +waveform=$(BUILD)/replay-waveform.vcd
	$(PYTHON) -B replay_audit.py --depth 16 --directed $(BUILD)/depth-16.stim $(BUILD)/waveform.trace
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/replay-waveform.vcd waveform.svg \
		--start 590 --end 950 --title "Replay holds a pending beat through pause and stop" \
		--signal '$(WAVE_SCOPE).clk=Clock' --signal '$(WAVE_SCOPE).cfg_valid=Command valid' \
		--signal '$(WAVE_SCOPE).cfg_op=Command opcode' --signal '$(WAVE_SCOPE).paused=Paused' \
		--signal '$(WAVE_SCOPE).now_tick=Virtual tick' --signal '$(WAVE_SCOPE).m_valid=Output valid' \
		--signal '$(WAVE_SCOPE).m_ready=Sink ready' --signal '$(WAVE_SCOPE).m_tag=Output tag' \
		--signal '$(WAVE_SCOPE).done=Done'

waveform-banked: $(BUILD)/banked.vvp
	$(PYTHON) -B gen_banked_replay_vectors.py $(BUILD)/banked-directed.vec --cycles 0
	$(VVP) $< +vectors=$(BUILD)/banked-directed.vec +waves=$(BUILD)/banked-replay.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/banked-replay.vcd banked-waveform.svg \
		--start 155 --end 425 --title 'Inactive tape staging and safe run handoff' \
		--signal '$(BANKED_TB).clk=Clock' --signal '$(BANKED_TB).cfg_valid=Command Valid' \
		--signal '$(BANKED_TB).cfg_bank=Command Bank' --signal '$(BANKED_TB).dut.pending=Reservation' \
		--signal '$(BANKED_TB).m_run_id=Active Run ID' --signal '$(BANKED_TB).m_valid=Output Valid' \
		--signal '$(BANKED_TB).m_ready=Sink Ready' --signal '$(BANKED_TB).run_started=Run Started' \
		--signal '$(BANKED_TB).done=Run Retired' --signal '$(BANKED_TB).done_truncated=Last Run Truncated'

.PHONY: test-impairment test-impairment-demo lint-impairment synth-impairment formal-impairment waveform-impairment

$(BUILD)/impairment.vvp: $(IMPAIRMENT_TB).sv $(IMPAIRMENT_RTL) impairment.f | $(BUILD)
	$(IVERILOG) -g2012 -s $(IMPAIRMENT_TB) -o $@ -f impairment.f $<

$(BUILD)/impairment-single.vvp: $(IMPAIRMENT_TB).sv $(IMPAIRMENT_RTL) impairment.f | $(BUILD)
	$(IVERILOG) -g2012 -s $(IMPAIRMENT_TB) -P$(IMPAIRMENT_TB).QUEUE_DEPTH=1 -o $@ -f impairment.f $<

$(BUILD)/impairment-default.vvp: $(IMPAIRMENT_TB).sv $(IMPAIRMENT_RTL) impairment.f | $(BUILD)
	$(IVERILOG) -g2012 -s $(IMPAIRMENT_TB) -P$(IMPAIRMENT_TB).QUEUE_DEPTH=4 -P$(IMPAIRMENT_TB).MAX_BEATS=16 \
		-P$(IMPAIRMENT_TB).RUN_W=32 -P$(IMPAIRMENT_TB).REV_W=16 -o $@ -f impairment.f $<

$(BUILD)/impairment-demo.vvp: tb_banked_impairment_demo.sv $(RTL) $(IMPAIRMENT_RTL) rtl.f impairment.f replay_codes.svh | $(BUILD)
	$(IVERILOG) -g2012 -s $(IMPAIRMENT_SCOPE) -o $@ -f rtl.f -f impairment.f $<

test-impairment-demo: $(BUILD)/impairment-demo.vvp
	$(PYTHON) -B replay_impairment_demo.py seed $(IMPAIRMENT_DEMO)
	$(PYTHON) -B replay_impairment_demo.py build $(IMPAIRMENT_DEMO)
	$(VVP) $< +out=$(IMPAIRMENT_DEMO)
	$(PYTHON) -B replay_impairment_demo.py check $(IMPAIRMENT_DEMO)

test-impairment: $(BUILD)/impairment.vvp $(BUILD)/impairment-single.vvp $(BUILD)/impairment-default.vvp test-impairment-demo
	$(PYTHON) -B gen_impairment_vectors.py $(BUILD)/impairment.txt
	$(VVP) $< +vectors=$(BUILD)/impairment.txt
	$(PYTHON) -B gen_impairment_vectors.py $(BUILD)/impairment-single.txt --queue 1 --seed 52341
	$(VVP) $(BUILD)/impairment-single.vvp +vectors=$(BUILD)/impairment-single.txt
	$(PYTHON) -B gen_impairment_vectors.py $(BUILD)/impairment-default.txt --queue 4 --maximum 16 --run-width 32 --revision-width 16 --seed 61477
	$(VVP) $(BUILD)/impairment-default.vvp +vectors=$(BUILD)/impairment-default.txt

lint-impairment:
	$(VERILATOR) --lint-only -Wall --top-module $(IMPAIRMENT_TOP) -f impairment.f

synth-impairment: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/synth-impairment.log -m slang -p 'read_slang --top $(IMPAIRMENT_TOP) $(IMPAIRMENT_RTL); synth -top $(IMPAIRMENT_TOP) -noabc; check -assert; stat'

formal-impairment: | $(BUILD)
	cd formal && $(SBY) -f --prefix ../$(BUILD)/formal-impairment replay_impairment.sby

waveform-impairment: $(BUILD)/impairment-demo.vvp
	$(PYTHON) -B replay_impairment_demo.py seed $(BUILD)/impairment-wave
	$(PYTHON) -B replay_impairment_demo.py build $(BUILD)/impairment-wave
	$(VVP) $< +out=$(BUILD)/impairment-wave +waves=$(BUILD)/impairment-wave/live.vcd
	$(PYTHON) -B replay_impairment_demo.py check $(BUILD)/impairment-wave
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/impairment-wave/live.vcd impairment-waveform.svg \
		--start 790 --end 1135 --title 'Replay drops a frame and preserves both copies through stalls' \
		--signal '$(IMPAIRMENT_SCOPE).s_tag=Input Tag' --signal '$(IMPAIRMENT_SCOPE).s_ready=Input Ready' \
		--signal '$(IMPAIRMENT_SCOPE).drops=Dropped Frames' --signal '$(IMPAIRMENT_SCOPE).occupancy=Queued Frames' \
		--signal '$(IMPAIRMENT_SCOPE).m_valid=Output Valid' --signal '$(IMPAIRMENT_SCOPE).m_ready=Sink Ready' \
		--signal '$(IMPAIRMENT_SCOPE).m_tag=Output Tag' --signal '$(IMPAIRMENT_SCOPE).m_copy=Second Copy' \
		--signal '$(IMPAIRMENT_SCOPE).m_action=Output Action'

clean:
	rm -rf $(BUILD)

.SECONDARY:
