TOP := drop_copy_reconciliation
TB := tb_drop_copy_reconciliation
BUILD := build
PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
SBY ?= sby
RTL := $(shell cat rtl.f)
HEADERS := reconcile_defs.svh

.PHONY: all test lint synth formal clean wave-vcd waveform
all: test

$(BUILD):
	mkdir -p $@

$(BUILD)/reconcile-%.vvp: $(RTL) $(HEADERS) $(TB).sv rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I. -s $(TB) -P $(TB).SETS=$* -o $@ -f rtl.f $(TB).sv

test: $(BUILD)/reconcile-1.vvp $(BUILD)/reconcile-4.vvp $(BUILD)/reconcile-8.vvp
	$(PYTHON) -B -m unittest discover -v
	@set -e; for depth in 1 4 8; do \
		$(PYTHON) -B check_drop_copy.py generate $(BUILD)/sets-$$depth --sets $$depth --seed $$depth; \
		$(VVP) $(BUILD)/reconcile-$$depth.vvp +vectors=$(BUILD)/sets-$$depth; \
		$(PYTHON) -B check_drop_copy.py check $(BUILD)/sets-$$depth; \
	done

lint:
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -f rtl.f

synth: | $(BUILD)
	$(YOSYS) -Q -q -m slang -l $(BUILD)/synth.log -p 'read_slang -I. --top $(TOP) $(RTL); synth -top $(TOP) -noabc; check -assert; stat'
	tail -n 22 $(BUILD)/synth.log

formal: | $(BUILD)
	$(SBY) -f --prefix $(BUILD)/formal drop_copy.sby

wave-vcd: $(BUILD)/reconcile-4.vvp
	$(PYTHON) -B check_drop_copy.py generate $(BUILD)/wave --cycles 0
	$(VVP) $< +vectors=$(BUILD)/wave +waves=$(BUILD)/wave/reconcile.vcd
	$(PYTHON) -B check_drop_copy.py check $(BUILD)/wave

waveform: wave-vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/wave/reconcile.vcd waveform.svg \
		--start 25 --end 115 --title 'Drop-copy match and same-lane conflict' \
		--signal 'tb_drop_copy_reconciliation.clk=Clock' \
		--signal 'tb_drop_copy_reconciliation.s_primary_valid=Primary Valid' \
		--signal 'tb_drop_copy_reconciliation.s_copy_valid=Copy Valid' \
		--signal 'tb_drop_copy_reconciliation.s_copy_ready=Copy Ready' \
		--signal 'tb_drop_copy_reconciliation.m_valid=Report Valid' \
		--signal 'tb_drop_copy_reconciliation.m_ready=Report Ready' \
		--signal 'tb_drop_copy_reconciliation.m_status=Report Status' \
		--signal 'tb_drop_copy_reconciliation.m_diff=Field Diff' \
		--signal 'tb_drop_copy_reconciliation.pending_count=Pending Keys'

clean:
	rm -rf $(BUILD)

.PHONY: test-partitions lint-partitions synth-partitions formal-partitions partition-waveform

PARTITION_TB := tb_drop_copy_partitions
PARTITION_TOP := drop_copy_partitions
PARTITION_TESTS := test-partitions-1 test-partitions-3 test-partitions-5
PARTITION_SETS_1 := 1
PARTITION_SETS_3 := 4
PARTITION_SETS_5 := 8

.PHONY: $(PARTITION_TESTS)
test: test-partitions
lint: lint-partitions
synth: synth-partitions
formal: formal-partitions
waveform: partition-waveform

test-partitions: $(PARTITION_TESTS)

$(PARTITION_TESTS): test-partitions-%: $(BUILD)/partitions-%.vvp
	$(PYTHON) -B check_partitions.py generate $(BUILD)/partition-$* --namespaces $* --sets $(PARTITION_SETS_$*) --seed $*
	$(VVP) $< +vectors=$(BUILD)/partition-$*
	$(PYTHON) -B check_partitions.py check $(BUILD)/partition-$*

$(BUILD)/partitions-%.vvp: $(RTL) $(HEADERS) $(PARTITION_TB).sv rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I. -s $(PARTITION_TB) -P $(PARTITION_TB).NAMESPACES=$* \
		-P $(PARTITION_TB).SETS=$(PARTITION_SETS_$*) -o $@ -f rtl.f $(PARTITION_TB).sv

lint-partitions:
	$(VERILATOR) --lint-only -Wall --top-module $(PARTITION_TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module $(PARTITION_TOP) -GNAMESPACES=1 -GSETS=1 -f rtl.f

synth-partitions: | $(BUILD)
	$(YOSYS) -Q -q -m slang -l $(BUILD)/partitions-synth.log -p 'read_slang -I. --top $(PARTITION_TOP) $(RTL); synth -top $(PARTITION_TOP) -noabc; check -assert; stat'

formal-partitions: | $(BUILD)
	$(YOSYS) -Q -q -m slang -l $(BUILD)/partitions-formal.log -p 'read_slang --formal -I. --top formal_drop_copy_partitions $(RTL) formal_drop_copy_partitions.sv; prep -top formal_drop_copy_partitions; memory_map; flatten; async2sync; chformal -lower; opt; sat -seq 8 -set-at 1 rst 1 -prove-asserts -verify'

partition-waveform: $(BUILD)/partitions-3.vvp
	$(PYTHON) -B check_partitions.py generate $(BUILD)/partition-wave --cycles 0
	$(VVP) $< +vectors=$(BUILD)/partition-wave +waves=$(BUILD)/partition-wave/session.vcd
	$(PYTHON) -B check_partitions.py check $(BUILD)/partition-wave
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/partition-wave/session.vcd partition-waveform.svg \
		--start 17 --end 260 --title 'Independent reconciliation and a targeted namespace restart' \
		--signal 'tb_drop_copy_partitions.s_primary_valid=Primary valid' \
		--signal 'tb_drop_copy_partitions.s_copy_valid=Copy valid' \
		--signal 'tb_drop_copy_partitions.m_ready=Audit ready' \
		--signal 'tb_drop_copy_partitions.m_valid=Audit valid' \
		--signal 'tb_drop_copy_partitions.m_namespace=Report namespace' \
		--signal 'tb_drop_copy_partitions.m_status=Report code' \
		--signal 'tb_drop_copy_partitions.s_epoch_valid=Epoch request valid' \
		--signal 'tb_drop_copy_partitions.namespace_incidents=Incident bitmap' \
		--signal 'tb_drop_copy_partitions.active_epochs=Packed epochs'

.PHONY: test-investigator lint-investigator synth-investigator formal-investigator investigation-waveform

AUDIT_TOP := drop_copy_investigator
AUDIT_TB := tb_drop_copy_investigator
AUDIT_TESTS := test-investigator-1 test-investigator-3 test-investigator-5
AUDIT_CYCLES ?= 9600

.PHONY: $(AUDIT_TESTS)
test: test-investigator
lint: lint-investigator
synth: synth-investigator
formal: formal-investigator
waveform: investigation-waveform

test-investigator: $(AUDIT_TESTS)

$(AUDIT_TESTS): test-investigator-%: $(BUILD)/investigator-%.vvp
	$(PYTHON) -B check_investigator.py generate $(BUILD)/investigator-$* \
		--namespaces $* --sets $(PARTITION_SETS_$*) --seed $* --cycles $(AUDIT_CYCLES)
	$(VVP) $< +vectors=$(BUILD)/investigator-$*
	$(PYTHON) -B check_investigator.py check $(BUILD)/investigator-$* --namespaces $* --sets $(PARTITION_SETS_$*)

$(BUILD)/investigator-%.vvp: $(RTL) $(HEADERS) $(AUDIT_TB).sv rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I. -s $(AUDIT_TB) -P $(AUDIT_TB).NAMESPACES=$* \
		-P $(AUDIT_TB).SETS=$(PARTITION_SETS_$*) -o $@ -f rtl.f $(AUDIT_TB).sv

lint-investigator:
	$(VERILATOR) --lint-only -Wall --top-module $(AUDIT_TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module $(AUDIT_TOP) -GNAMESPACES=1 -GSETS=1 -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module drop_copy_audit_frame -f rtl.f

synth-investigator: | $(BUILD)
	$(YOSYS) -Q -q -m slang -l $(BUILD)/investigator-synth.log -p 'read_slang -I. --top $(AUDIT_TOP) $(RTL); synth -top $(AUDIT_TOP) -noabc; check -assert; stat'
	$(YOSYS) -Q -q -m slang -l $(BUILD)/audit-frame-synth.log -p 'read_slang --top drop_copy_audit_frame drop_copy_audit_frame.sv; synth -top drop_copy_audit_frame -noabc; check -assert; stat'

formal-investigator: | $(BUILD)
	$(YOSYS) -Q -q -m slang -l $(BUILD)/investigator-formal.log -p 'read_slang --formal -D FORMAL -I. --top $(AUDIT_TOP) -G NAMESPACES=1 -G SETS=1 -G WAIT_CYCLES=3 -G HISTORY_CYCLES=5 $(RTL); prep; memory_map; flatten; async2sync; chformal -lower; opt; sat -seq 9 -set-at 1 rst 1 -prove-asserts -verify'
	$(YOSYS) -Q -q -m slang -l $(BUILD)/audit-frame-formal.log -p 'read_slang --formal -D FORMAL --top drop_copy_audit_frame -G RECORD_W=39 drop_copy_audit_frame.sv; prep; memory_map; flatten; async2sync; chformal -lower; opt; sat -seq 12 -set-at 1 rst 1 -prove-asserts -verify'

investigation-waveform: $(BUILD)/investigator-3.vvp
	$(PYTHON) -B check_investigator.py generate $(BUILD)/investigation-wave --cycles 0
	$(VVP) $< +vectors=$(BUILD)/investigation-wave +waves=$(BUILD)/investigation-wave/tape.vcd
	$(PYTHON) -B check_investigator.py check $(BUILD)/investigation-wave
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/investigation-wave/tape.vcd investigation-waveform.svg \
		--start 8 --end 630 --title 'Recorder backpressure preserves a missing-copy and late-mate timeline' \
		--signal 'tb_drop_copy_investigator.s_primary_valid=Primary offer' \
		--signal 'tb_drop_copy_investigator.audit_allow=Recorder enabled' \
		--signal 'tb_drop_copy_investigator.audit_ready=Recorder ready' \
		--signal 'tb_drop_copy_investigator.audit_index=Recorded index' \
		--signal 'tb_drop_copy_investigator.m_valid=Diagnostic valid' \
		--signal 'tb_drop_copy_investigator.m_status=Diagnostic code' \
		--signal 'tb_drop_copy_investigator.s_copy_valid=Late copy offered' \
		--signal 'tb_drop_copy_investigator.s_copy_ready=Late copy ready' \
		--signal 'tb_drop_copy_investigator.m_record_index=Diagnostic record' \
		--signal 'tb_drop_copy_investigator.namespace_incidents=Incident bitmap'
