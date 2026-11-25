        logic initialized = 0;
        always @(posedge clk) begin
            initialized <= 1;
            if (initialized && !rst && $past(!rst)) begin
                assert(!inserted || (armed && due));
                assert(!auto_insert || (!external_offer && !ingress_ready[instrument]));
                assert(!discard || !dispatch);
                if (external_offer && due)
                    assert(input_reason == `AB_CUTOFF);
                if (set_arm)
                    assert(!request_valid && worker_ready && collecting && !armed && ctl_cutoff > clock_tick);
                if (clear_arm)
                    assert(armed && !due);
                if (worker_valid && working_schedule) begin
                    assert(inserted);
                    assert(epoch_out == arm_epoch && tag_out == arm_tag);
                    assert(kind != `AU_EV_REJECT);
                end
                if ($past(auto_insert && input_credit))
                    assert(inserted && armed);
                if ($past(auto_insert && !input_credit))
                    assert(due && !inserted);
                if (!$past(armed) && !$past(set_arm))
                    assert(!armed);
            end
        end
