PYTHON ?= python3
IVERILOG ?= iverilog
VVP ?= vvp
VERILATOR ?= verilator
YOSYS ?= yosys
BUILD := build
TOP := host_dma_queues
RTL := $(shell cat rtl.f)
HEADERS := dma_protocol.svh
MEMORY_MODEL := tb_dma_memory.sv
INTEGRATION_HEADERS := tb_dma_readback.svh tb_dma_scatter.svh tb_dma_checkpoint.svh

.PHONY: waveform wave-vcd waveform-readback waveform-scatter all test regress icarus test-host test-components test-integration test-readback test-isolation test-scatter lint synth clean
.PHONY: test-checkpoint checkpoint-capture waveform-checkpoint
all: test
regress icarus: test
test: test-host test-components test-integration test-readback test-isolation test-scatter test-checkpoint

test-host: | $(BUILD)
	$(PYTHON) -B -m unittest discover -v
	$(PYTHON) -B dma_workload.py --seed 8088 --requests 96 --depth 8 --output $(BUILD)/ledger.json

test-components: $(BUILD)/tb_dma_axi64.vvp $(BUILD)/tb_dma_axi64_max.vvp $(BUILD)/tb_dma_queue_table.vvp $(BUILD)/tb_dma_crc_irq.vvp
	@set -e; for bench in $^; do $(VVP) "$$bench"; done

test-integration: $(BUILD)/tb_host_dma_queues.vvp $(BUILD)/tb_dma_contention.vvp
	$(VVP) $(BUILD)/tb_host_dma_queues.vvp +capture=$(BUILD)
	$(PYTHON) -B audit_directed_capture.py $(BUILD)
	$(VVP) $(BUILD)/tb_dma_contention.vvp

test-readback: $(BUILD)/tb_host_dma_queues.vvp
	mkdir -p $(BUILD)/readback
	$(VVP) $< +readback +capture=$(BUILD)/readback
	$(PYTHON) -B audit_dma_readback.py $(BUILD)/readback

test-isolation: $(BUILD)/tb_dma_queue_isolation.vvp $(BUILD)/tb_host_dma_isolation.vvp
	$(VVP) $(BUILD)/tb_dma_queue_isolation.vvp
	$(VVP) $(BUILD)/tb_host_dma_isolation.vvp +isolation-access
	mkdir -p $(BUILD)/isolation
	$(VVP) $(BUILD)/tb_host_dma_isolation.vvp +readback +capture=$(BUILD)/isolation
	$(PYTHON) -B audit_dma_readback.py $(BUILD)/isolation

test-scatter: $(BUILD)/tb_host_dma_queues.vvp $(BUILD)/tb_host_dma_seg1.vvp $(BUILD)/tb_host_dma_seg3.vvp
	mkdir -p $(BUILD)/scatter
	$(VVP) $< +scatter +capture=$(BUILD)/scatter
	$(PYTHON) -B audit_dma_scatter.py $(BUILD)/scatter
	@set -e; for bench in $^; do $(VVP) "$$bench" +segment-capacity; done

test-checkpoint: checkpoint-capture
	$(PYTHON) -B audit_dma_checkpoint.py $(BUILD)/checkpoint

checkpoint-capture: $(BUILD)/tb_host_dma_queues.vvp
	mkdir -p $(BUILD)/checkpoint
	$(VVP) $< +checkpoint +checkpoint-dir=$(BUILD)/checkpoint +waves=$(BUILD)/checkpoint/checkpoint.vcd

waveform-checkpoint: test-checkpoint ../scripts/render_vcd_waveform.py
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/checkpoint/checkpoint.vcd checkpoint_waveform.svg \
		--start 115 --end 1080 --title 'Day 08 · Crash recovery follows committed ring cursors' \
		--signal 'tb_host_dma_queues.checkpoint_phase=Snapshot count' \
		--signal 'tb_host_dma_queues.block_write_response=Memory holds B' \
		--signal 'tb_host_dma_queues.wlast=Last CQ write beat' \
		--signal 'tb_host_dma_queues.bvalid=AXI B valid' \
		--signal 'tb_host_dma_queues.host_valid=Host doorbell valid' \
		--signal 'tb_host_dma_queues.inspect_sq_tail=Published SQ tail' \
		--signal 'tb_host_dma_queues.inspect_cq_tail=Committed CQ tail' \
		--signal 'tb_host_dma_queues.inspect_cq_head=Released CQ head'

waveform-readback: $(BUILD)/tb_host_dma_queues.vvp ../scripts/render_vcd_waveform.py
	mkdir -p $(BUILD)/readback-wave
	$(VVP) $< +readback +capture=$(BUILD)/readback-wave +waves=$(BUILD)/readback-wave/readback.vcd
	$(PYTHON) -B audit_dma_readback.py $(BUILD)/readback-wave
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/readback-wave/readback.vcd readback_waveform.svg \
		--start 1090 --end 1855 --title 'Verified PUSH waits for payload B, readback, and CQ B' \
		--signal 'tb_host_dma_queues.dut.state=DMA worker state' \
		--signal 'tb_host_dma_queues.block_write_response=Memory holds B' \
		--signal 'tb_host_dma_queues.bvalid=AXI B valid' \
		--signal 'tb_host_dma_queues.bready=AXI B ready' \
		--signal 'tb_host_dma_queues.arvalid=AXI AR valid' \
		--signal 'tb_host_dma_queues.rvalid=AXI R valid' \
		--signal 'tb_host_dma_queues.rlast=AXI R last' \
		--signal 'tb_host_dma_queues.awvalid=AXI AW valid' \
		--signal 'tb_host_dma_queues.inspect_cq_tail=Queue 0 CQ tail' \
		--signal 'tb_host_dma_queues.irq=IRQ'

waveform-scatter: $(BUILD)/tb_host_dma_queues.vvp ../scripts/render_vcd_waveform.py
	mkdir -p $(BUILD)/scatter-wave
	$(VVP) $< +scatter +capture=$(BUILD)/scatter-wave +waves=$(BUILD)/scatter-wave/scatter.vcd
	$(PYTHON) -B audit_dma_scatter.py $(BUILD)/scatter-wave
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/scatter-wave/scatter.vcd scatter_waveform.svg \
		--start 2490 --end 3710 --title 'Verified scatter waits for every payload response before readback' \
		--signal 'tb_host_dma_queues.dut.state=DMA worker state' \
		--signal 'tb_host_dma_queues.dut.segment=Segment index' \
		--signal 'tb_host_dma_queues.block_write_response=Memory holds B' \
		--signal 'tb_host_dma_queues.awvalid=AXI AW valid' \
		--signal 'tb_host_dma_queues.bvalid=AXI B valid' \
		--signal 'tb_host_dma_queues.arvalid=AXI AR valid' \
		--signal 'tb_host_dma_queues.inspect_cq_tail=Queue 1 CQ tail' \
		--signal 'tb_host_dma_queues.irq=Interrupt levels'

$(BUILD):
	mkdir -p $@

$(BUILD)/tb_dma_axi64_max.vvp: tb_dma_axi64.sv $(RTL) $(HEADERS) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I . -s tb_dma_axi64 -Ptb_dma_axi64.MAX_BURST=16 -o $@ -f rtl.f $<

$(BUILD)/tb_dma_queue_isolation.vvp: tb_dma_queue_table.sv $(RTL) $(HEADERS) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I . -s tb_dma_queue_table -Ptb_dma_queue_table.ENFORCE_QUEUE_ISOLATION=1 -o $@ -f rtl.f $<

$(BUILD)/tb_host_dma_isolation.vvp: tb_host_dma_queues.sv $(INTEGRATION_HEADERS) $(MEMORY_MODEL) $(RTL) $(HEADERS) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I . -s tb_host_dma_queues -Ptb_host_dma_queues.ENFORCE_QUEUE_ISOLATION=1 -o $@ -f rtl.f $< $(MEMORY_MODEL)

$(BUILD)/tb_host_dma_seg%.vvp: tb_host_dma_queues.sv $(INTEGRATION_HEADERS) $(MEMORY_MODEL) $(RTL) $(HEADERS) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I . -s tb_host_dma_queues -Ptb_host_dma_queues.TEST_SEGMENTS=$* -o $@ -f rtl.f $< $(MEMORY_MODEL)

$(BUILD)/tb_host_dma_queues.vvp $(BUILD)/tb_dma_contention.vvp: $(BUILD)/tb_%.vvp: tb_%.sv $(MEMORY_MODEL) $(RTL) $(HEADERS) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I . -s tb_$* -o $@ -f rtl.f $< $(MEMORY_MODEL)

$(BUILD)/tb_host_dma_queues.vvp: $(INTEGRATION_HEADERS)

$(BUILD)/tb_%.vvp: tb_%.sv $(RTL) $(HEADERS) rtl.f | $(BUILD)
	$(IVERILOG) -g2012 -I . -s tb_$* -o $@ -f rtl.f $<

lint:
	$(VERILATOR) --lint-only -Wall -Wno-PINCONNECTEMPTY -I. --top-module $(TOP) -f rtl.f
	$(VERILATOR) --lint-only -Wall -Wno-PINCONNECTEMPTY -I. --top-module $(TOP) -GENFORCE_QUEUE_ISOLATION=1 -f rtl.f

synth: | $(BUILD)
	$(YOSYS) -Q -q -l $(BUILD)/synth.log -m slang -p 'read_slang -I. --top $(TOP) $(RTL); synth -top $(TOP) -noabc; check -assert; stat'
	tail -n 22 $(BUILD)/synth.log
	$(YOSYS) -Q -q -l $(BUILD)/synth_isolation.log -m slang -p 'read_slang -I. --top $(TOP) -G ENFORCE_QUEUE_ISOLATION=1 $(RTL); synth -top $(TOP) -noabc; check -assert; stat'
	tail -n 22 $(BUILD)/synth_isolation.log

wave-vcd: $(BUILD)/tb_host_dma_queues.vvp
	mkdir -p $(BUILD)/wave
	$(VVP) $< +capture=$(BUILD)/wave +waves=$(BUILD)/wave/dma.vcd
	$(PYTHON) -B audit_directed_capture.py $(BUILD)/wave

waveform: wave-vcd
	$(PYTHON) -B ../scripts/render_vcd_waveform.py $(BUILD)/wave/dma.vcd waveform.svg \
		--start 8990 --end 9195 --title 'Completion publication waits for the AXI write response' \
		--signal 'tb_host_dma_queues.clk=Clock' \
		--signal 'tb_host_dma_queues.block_write_response=Memory Holds B' \
		--signal 'tb_host_dma_queues.wvalid=AXI W Valid' \
		--signal 'tb_host_dma_queues.wready=AXI W Ready' \
		--signal 'tb_host_dma_queues.wlast=AXI W Last' \
		--signal 'tb_host_dma_queues.bvalid=AXI B Valid' \
		--signal 'tb_host_dma_queues.bready=AXI B Ready' \
		--signal 'tb_host_dma_queues.inspect_cq_tail=Queue 0 CQ Tail'

clean:
	rm -rf $(BUILD)
