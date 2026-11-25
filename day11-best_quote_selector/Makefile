PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
TOP = best_quote_selector
SOURCES = $(shell cat rtl.f)
PROFILES = default solo odd wide
IMAGES = $(addprefix build/,$(addsuffix .vvp,$(PROFILES)))

.PHONY: test test-rtl test-python lint synth clean

test: test-python test-rtl

test-python:
	$(PYTHON) -B -m unittest discover -v

build:
	mkdir -p $@

build/%.vvp: $(SOURCES) quote_codes.svh tb_best_quote_selector.sv quote_schema.py quote_tool.py | build
	$(IVERILOG) -g2012 -s tb_best_quote_selector `$(PYTHON) -B quote_tool.py parameters --profile $*` -o $@ -c rtl.f tb_best_quote_selector.sv

build/rank.vvp: quote_reduce_side.sv tb_quote_reduce_side.sv | build
	$(IVERILOG) -g2012 -s tb_quote_reduce_side -o $@ $^

test-rtl: $(IMAGES) build/rank.vvp
	$(VVP) build/rank.vvp
	@set -e; for profile in $(PROFILES); do \
		$(PYTHON) -B quote_tool.py regress --profile "$$profile" --image "build/$$profile.vvp" --vvp "$(VVP)" --output "build/regression/$$profile"; \
	done

lint:
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module $(TOP) -GINSTRUMENTS=1 -GVENUES=1 -f rtl.f

synth: | build
	$(YOSYS) -Q -l build/synth.log -p 'read_verilog -sv $(SOURCES); hierarchy -check -top $(TOP); synth -top $(TOP) -noabc; check -assert; stat'

clean:
	rm -rf build obj_dir

.PHONY: formal

formal: | build
	$(YOSYS) -Q -q -l build/formal-rank.log -p 'read_verilog -formal -sv quote_reduce_side.sv tb_quote_rank_formal.sv; hierarchy -check -top formal_quote_rank; proc; flatten; chformal -lower; opt; sat -prove-asserts -verify'
	$(YOSYS) -Q -q -l build/formal-flow.log -p 'read_verilog -formal -sv $(SOURCES) tb_quote_flow_formal.sv; hierarchy -check -top formal_quote_flow; proc; flatten; memory_map; async2sync; chformal -lower; opt; sat -seq 7 -set-at 1 rst 1 -prove-asserts -verify'

.PHONY: demo

test: demo

demo: build/default.vvp
	$(PYTHON) -B quote_demo.py --image build/default.vvp --vvp "$(VVP)"

.PHONY: waveform

waveform: build/default.vvp ../scripts/render_vcd_waveform.py
	$(PYTHON) -B quote_demo.py --image $< --output build/waveform --vvp "$(VVP)" --waveform build/best_quote_selector.vcd
	mkdir -p docs
	$(PYTHON) -B ../scripts/render_vcd_waveform.py build/best_quote_selector.vcd docs/waveform.svg \
		--start 8 --end 49 --title 'Day 11 · Quote updates and a crossed market' \
		--signal 'tb_best_quote_selector.clk=Clock' \
		--signal 'tb_best_quote_selector.cmd_valid=Update valid' \
		--signal 'tb_best_quote_selector.query_valid=Query valid' \
		--signal 'tb_best_quote_selector.result_valid=Result valid' \
		--signal 'tb_best_quote_selector.result_tag=Query tag' \
		--signal 'tb_best_quote_selector.result_state=Market state' \
		--signal 'tb_best_quote_selector.result_bid_price=Best bid' \
		--signal 'tb_best_quote_selector.result_ask_price=Best ask' \
		--signal 'tb_best_quote_selector.result_spread=Signed spread bits'

.PHONY: test-sweep lint-sweep synth-sweep sweep-waveform

SWEEP_SOURCES = quote_store_engine.sv quote_store.sv quote_fee_policy.sv quote_sweep_plan.sv quote_sweep_selector.sv
SWEEP_IMAGES = $(addprefix build/sweep-,$(addsuffix .vvp,$(PROFILES)))
SWEEP_TESTS = $(addprefix test-sweep-,$(PROFILES))
.PHONY: $(SWEEP_TESTS)

test: test-sweep
lint: lint-sweep
synth: synth-sweep
waveform: sweep-waveform

test-sweep: $(SWEEP_TESTS)

$(SWEEP_TESTS): test-sweep-%: build/sweep-%.vvp
	$(PYTHON) -B quote_sweep_vectors.py --profile $* --image $< --output build/sweep-$* --vvp "$(VVP)"

$(SWEEP_IMAGES): build/sweep-%.vvp: $(SWEEP_SOURCES) quote_codes.svh quote_sweep_codes.svh tb_quote_sweep_selector.sv | build
	@set -e; params=$$($(PYTHON) -B quote_tool.py parameters --profile $*); \
	params=$$(printf '%s\n' "$$params" | sed 's/tb_best_quote_selector/tb_quote_sweep_selector/g'); \
	$(IVERILOG) -g2012 -s tb_quote_sweep_selector $$params -o $@ $(SWEEP_SOURCES) tb_quote_sweep_selector.sv

lint-sweep:
	$(VERILATOR) --lint-only -Wall --top-module quote_sweep_selector $(SWEEP_SOURCES)
	$(VERILATOR) --lint-only -Wall --top-module quote_sweep_selector -GINSTRUMENTS=1 -GVENUES=1 $(SWEEP_SOURCES)

synth-sweep: | build
	$(YOSYS) -Q -q -l build/sweep-synth.log -p 'read_verilog -sv $(SWEEP_SOURCES); hierarchy -check -top quote_sweep_selector; synth -top quote_sweep_selector -noabc; check -assert; stat'

sweep-waveform: build/sweep-default.vvp ../scripts/render_vcd_waveform.py
	$(PYTHON) -B quote_sweep_vectors.py --profile default --image $< --output build/sweep-demo --vvp "$(VVP)" --demo --waveform build/sweep-demo.vcd
	mkdir -p docs
	$(PYTHON) -B ../scripts/render_vcd_waveform.py build/sweep-demo.vcd docs/sweep-waveform.svg \
		--start 14 --end 113 --title 'Fee-aware sweep captures quotes and policy before the commit edge' \
		--signal 'tb_quote_sweep_selector.commit_valid=Policy commit' \
		--signal 'tb_quote_sweep_selector.policy_epoch=Active policy epoch' \
		--signal 'tb_quote_sweep_selector.query_valid=Query valid' \
		--signal 'tb_quote_sweep_selector.result_ready=Result ready' \
		--signal 'tb_quote_sweep_selector.result_valid=Result valid' \
		--signal 'tb_quote_sweep_selector.result_epoch=Sampled epoch' \
		--signal 'tb_quote_sweep_selector.result_status=Outcome' \
		--signal 'tb_quote_sweep_selector.first_venue=First child venue' \
		--signal 'tb_quote_sweep_selector.result_quantity=Planned quantity'

.PHONY: test-reservation lint-reservation synth-reservation formal-reservation reservation-waveform

RESERVATION_TOP := quote_reserved_sweep
RESERVATION_TB := tb_quote_reserved_sweep
RESERVATION_TESTS := $(addprefix test-reservation-,$(PROFILES))
RESERVATION_IMAGES := $(addprefix build/reservation-,$(addsuffix .vvp,$(PROFILES)))
RESERVATION_SLOTS_default := 3
RESERVATION_SLOTS_solo := 1
RESERVATION_SLOTS_odd := 3
RESERVATION_SLOTS_wide := 5
RESERVATION_EDGES ?= 2200

.PHONY: $(RESERVATION_TESTS)

test: test-reservation
lint: lint-reservation
synth: synth-reservation
formal: formal-reservation
waveform: reservation-waveform

test-reservation: $(RESERVATION_TESTS)

$(RESERVATION_TESTS): test-reservation-%: build/reservation-%.vvp
	$(PYTHON) -B quote_reservation_vectors.py --profile $* --image $< \
		--output build/reservation-$* --vvp "$(VVP)" --cycles $(RESERVATION_EDGES)

$(RESERVATION_IMAGES): build/reservation-%.vvp: $(SOURCES) quote_codes.svh quote_sweep_codes.svh $(RESERVATION_TB).sv | build
	@set -e; params=$$($(PYTHON) -B quote_tool.py parameters --profile $*); \
	params=$$(printf '%s\n' "$$params" | sed 's/tb_best_quote_selector/tb_quote_reserved_sweep/g'); \
	$(IVERILOG) -g2012 -s $(RESERVATION_TB) $$params -P$(RESERVATION_TB).RES_SLOTS=$(RESERVATION_SLOTS_$*) \
		-o $@ -c rtl.f $(RESERVATION_TB).sv

lint-reservation:
	$(VERILATOR) --lint-only -Wall --top-module $(RESERVATION_TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall --top-module $(RESERVATION_TOP) -GINSTRUMENTS=1 -GVENUES=1 -GRES_SLOTS=1 -f rtl.f

synth-reservation: | build
	$(YOSYS) -Q -q -m slang -l build/reservation-synth.log -p 'read_slang --top $(RESERVATION_TOP) $(SOURCES); synth -top $(RESERVATION_TOP) -noabc; check -assert; stat'

formal-reservation: | build
	$(YOSYS) -Q -q -m slang -l build/reservation-formal.log -p 'read_slang --formal -D FORMAL --top quote_reservation_ledger -G SLOTS=2 -G INSTRUMENTS=1 -G VENUES=2 -G QTY_W=3 -G TIME_W=4 -G TAG_W=2 -G TOKEN_W=2 -G CHILD_W=3 quote_reservation_ledger.sv; prep; memory_map; flatten; async2sync; chformal -lower; opt; sat -seq 12 -set-at 1 rst 1 -prove-asserts -verify'

reservation-waveform: build/reservation-default.vvp ../scripts/render_vcd_waveform.py
	$(PYTHON) -B quote_reservation_vectors.py --profile default --image $< --output build/reservation-demo \
		--vvp "$(VVP)" --demo --waveform build/reservation-demo/dispatch.vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py build/reservation-demo/dispatch.vcd docs/reservation-waveform.svg \
		--start 18 --end 126 --title 'A same-edge quote replacement revokes a whole reserved dispatch' \
		--signal 'tb_quote_reserved_sweep.result_valid=Reservation result' \
		--signal 'tb_quote_reserved_sweep.result_quantity=Reserved shares' \
		--signal 'tb_quote_reserved_sweep.reservation_count=Live tokens' \
		--signal 'tb_quote_reserved_sweep.cmd_valid=Quote update' \
		--signal 'tb_quote_reserved_sweep.grant_valid=Grant valid' \
		--signal 'tb_quote_reserved_sweep.grant_ready=Dispatch ready' \
		--signal 'tb_quote_reserved_sweep.grant_applicable=Grant applicable' \
		--signal 'tb_quote_reserved_sweep.dispatched_total=Dispatched bundles' \
		--signal 'tb_quote_reserved_sweep.revoked_grants=Revoked grants'
