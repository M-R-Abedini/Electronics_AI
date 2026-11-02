# Digital and high-speed profile

Apply interface-specific obligations to verified pin groups and driver/receiver
directions. Nominal clock frequency alone is insufficient; obtain relevant edge
rates, stackup/reference layers, package/connector geometry and interface timing,
impedance and skew budgets from the selected devices and specification.

Check pad-facing and escape first, reserve per-layer channels and preserve
reference continuity. For transitions, verify the proposed return connection's
net and span. For differential groups, distinguish individual widths, pair gap,
uncoupled sections and timing constraints. A legal width calculation does not
prove routing or compliance. Reuse the existing HSD/impedance/length and copper
modules with qualified inputs; keep unsupported models unknown.

Termination placement follows the actual termination type and driver topology.
Do not place every resistor at a presumed source. Distinguish a passive crystal
network from an active oscillator; obtain loading, return and keepout guidance
for the exact selected implementation. No universal copper-removal area,
crystal distance, via count or 3W/5W spacing certifies every interface.

Compare critical path/escape nonregression before ordinary HPWL. Report
straight-line crossings and channel estimates as proxies. Trial routing and
post-route checks should use the same rules, obstructions and search budget
across alternatives. Combine analog and isolation obligations per affected
scope; a digital profile does not waive them.
