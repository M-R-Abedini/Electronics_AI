# فهرست skipهای واقعی P1

این فهرست از JUnit اجرای نهایی اصلاحات مستقل در Windows و Python 3.12 تولید شده است؛ هیچ تست حذف، غیرفعال یا برای عبور skip نشده است. دادهٔ synthetic در آزمون واحد، پذیرش برد واقعی شمرده نمی‌شود.

- ریشه: ۷۷۸ pass، صفر fail/error، صفر skip.
- eda-agent: ۴۷۴۶ pass، صفر fail/error، ۱۱۵ skip موجود.

| تعداد | دلیل دقیق pytest |
| --- | --- |
| 9 | live-Altium test: set EDA_AGENT_INTEGRATION=1 to run. Skipped without contacting the bridge, so a running Altium session is left untouched. |
| 1 | needs the local-only binary fixture EDAAgentTest_ICs.SchLib |
| 1 | needs the local-only binary fixture |
| 41 | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| 23 | Free Pascal Compiler (fpc) is not installed or not on PATH |
| 1 | got empty parameter set for (cls, method) |
| 2 | extension not built in this tree |
| 19 | the official reference is not cloned at reference/easyeda-api-skill |
| 10 | official reference not cloned |
| 6 | needs local-only binary fixture main.SchDoc |
| 2 | Free Pascal Compiler (fpc) not on PATH |

| مجموعه | نام تست | دلیل |
| --- | --- | --- |
| eda-agent | `tests.integration.test_round_trip.TestProtocolContract::test_ping_returns_protocol_version` | live-Altium test: set EDA_AGENT_INTEGRATION=1 to run. Skipped without contacting the bridge, so a running Altium session is left untouched. |
| eda-agent | `tests.integration.test_round_trip.TestProtocolContract::test_ping_returns_script_version` | live-Altium test: set EDA_AGENT_INTEGRATION=1 to run. Skipped without contacting the bridge, so a running Altium session is left untouched. |
| eda-agent | `tests.integration.test_round_trip.TestProtocolContract::test_unknown_command_returns_structured_error` | live-Altium test: set EDA_AGENT_INTEGRATION=1 to run. Skipped without contacting the bridge, so a running Altium session is left untouched. |
| eda-agent | `tests.integration.test_round_trip.TestApplicationSurface::test_get_open_documents` | live-Altium test: set EDA_AGENT_INTEGRATION=1 to run. Skipped without contacting the bridge, so a running Altium session is left untouched. |
| eda-agent | `tests.integration.test_round_trip.TestApplicationSurface::test_get_active_document` | live-Altium test: set EDA_AGENT_INTEGRATION=1 to run. Skipped without contacting the bridge, so a running Altium session is left untouched. |
| eda-agent | `tests.integration.test_round_trip.TestProjectSurface::test_get_focused_project_after_open` | live-Altium test: set EDA_AGENT_INTEGRATION=1 to run. Skipped without contacting the bridge, so a running Altium session is left untouched. |
| eda-agent | `tests.integration.test_round_trip.TestProjectSurface::test_compile_project` | live-Altium test: set EDA_AGENT_INTEGRATION=1 to run. Skipped without contacting the bridge, so a running Altium session is left untouched. |
| eda-agent | `tests.integration.test_round_trip.TestConcurrentRequests::test_concurrent_pings_dont_collide` | live-Altium test: set EDA_AGENT_INTEGRATION=1 to run. Skipped without contacting the bridge, so a running Altium session is left untouched. |
| eda-agent | `tests.integration.test_round_trip.TestErrorCodeMapping::test_no_pcb_loaded_raises_precondition_error` | live-Altium test: set EDA_AGENT_INTEGRATION=1 to run. Skipped without contacting the bridge, so a running Altium session is left untouched. |
| eda-agent | `tests.test_altium_local_provider::test_it_reads_a_real_schlib` | needs the local-only binary fixture EDAAgentTest_ICs.SchLib |
| eda-agent | `tests.test_altium_local_provider::test_search_filters_on_name_and_description` | needs the local-only binary fixture |
| eda-agent | `tests.test_bom_netlist_cli::test_bom_disabled_without_optin` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_bom_netlist_cli::test_bom_human_output` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_bom_netlist_cli::test_bom_csv_output` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_bom_netlist_cli::test_bom_project_via_env_var` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_bom_netlist_cli::test_netlist_disabled_without_optin` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_bom_netlist_cli::test_netlist_reports_shorts_and_fails` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_bom_netlist_cli::test_netlist_fail_on_never_is_zero` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_bom_netlist_cli::test_netlist_json_has_nets` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_bom_netlist_cli::test_netlist_sarif_output` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_cross_validate.TestCrossValidation::test_cross_validate_all` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestCrossValidateByFunction::test_extract_json_value` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestCrossValidateByFunction::test_extract_json_array` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestCrossValidateByFunction::test_escape_json_string` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestCrossValidateByFunction::test_build_success_response` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestCrossValidateByFunction::test_build_error_response` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestCrossValidateByFunction::test_coordinate_conversions` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestCrossValidateByFunction::test_string_helpers` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestCrossValidateByFunction::test_matches_filter` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestCrossValidateByFunction::test_build_object_json` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestCrossValidateByFunction::test_apply_set_properties` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestCrossValidateByFunction::test_count_batch_operations` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestCrossValidateByFunction::test_split_command` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestIeeePinSymbolsAgainstRealPascal::test_the_two_ordinals_that_carry_schematic_meaning` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestIeeePinSymbolsAgainstRealPascal::test_named_ordinals_match_the_api_reference` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestIeeePinSymbolsAgainstRealPascal::test_every_ordinal_survives_naming_and_reparsing` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestIeeePinSymbolsAgainstRealPascal::test_aliases_and_altium_raw_enum_spelling` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestIeeePinSymbolsAgainstRealPascal::test_bare_ordinals_pass_through_and_junk_falls_back` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_cross_validate.TestIeeePinSymbolsAgainstRealPascal::test_naming_an_ordinal_back` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_easyeda_api_calls_are_real::test_expected_absences_are_still_absent[NOTSET]` | got empty parameter set for (cls, method) |
| eda-agent | `tests.test_easyeda_build_is_current::test_the_check_passes_on_a_freshly_built_tree` | extension not built in this tree |
| eda-agent | `tests.test_easyeda_build_is_current::test_source_and_build_differ_in_bytes_which_is_why_that_test_failed` | extension not built in this tree |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_the_reference_actually_parsed` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_our_call_sites_were_actually_found` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_no_call_passes_more_arguments_than_the_api_declares[NOTSET]` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_every_method_we_call_exists_in_the_reference` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_instances_we_call_are_documented_classes` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_the_shim_call_sites_were_found` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_shim_calls_pass_an_argument_count_the_api_accepts[1107-None-check-3]` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_shim_calls_pass_an_argument_count_the_api_accepts[2016-dmt_Schematic-getAllSchematicsInfo-0]` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_shim_calls_pass_an_argument_count_the_api_accepts[2108-dmt_Schematic-getAllSchematicsInfo-0]` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_the_drc_confirmation_asks_for_the_array_overload` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_the_library_search_callers_were_found` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_every_dynamically_dispatched_search_exists[lib_3DModel]` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_every_dynamically_dispatched_search_exists[lib_Device]` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_every_dynamically_dispatched_search_exists[lib_Footprint]` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_every_dynamically_dispatched_search_exists[lib_Symbol]` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_the_symbol_type_split_matches_the_reference[lib_3DModel]` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_the_symbol_type_split_matches_the_reference[lib_Device]` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_the_symbol_type_split_matches_the_reference[lib_Footprint]` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_call_arity_matches_official::test_the_symbol_type_split_matches_the_reference[lib_Symbol]` | the official reference is not cloned at reference/easyeda-api-skill |
| eda-agent | `tests.test_easyeda_destructive_gate::test_every_gated_name_is_a_real_method[autoLayout]` | official reference not cloned |
| eda-agent | `tests.test_easyeda_destructive_gate::test_every_gated_name_is_a_real_method[autoRouting]` | official reference not cloned |
| eda-agent | `tests.test_easyeda_destructive_gate::test_every_gated_name_is_a_real_method[importAutoLayoutJsonFile]` | official reference not cloned |
| eda-agent | `tests.test_easyeda_destructive_gate::test_every_gated_name_is_a_real_method[modifyDifferentialPairName]` | official reference not cloned |
| eda-agent | `tests.test_easyeda_destructive_gate::test_every_gated_name_is_a_real_method[modifyDifferentialPairNegativeNet]` | official reference not cloned |
| eda-agent | `tests.test_easyeda_destructive_gate::test_every_gated_name_is_a_real_method[modifyDifferentialPairPositiveNet]` | official reference not cloned |
| eda-agent | `tests.test_easyeda_destructive_gate::test_every_gated_name_is_a_real_method[modifyEqualLengthNetGroupName]` | official reference not cloned |
| eda-agent | `tests.test_easyeda_destructive_gate::test_every_gated_name_is_a_real_method[modifyNetClassName]` | official reference not cloned |
| eda-agent | `tests.test_easyeda_destructive_gate::test_every_gated_name_is_a_real_method[restoreDefault]` | official reference not cloned |
| eda-agent | `tests.test_easyeda_marker_shapes::test_the_five_types_are_the_documented_enum` | official reference not cloned |
| eda-agent | `tests.test_fileio_bom::test_real_fixture_quantities_sum_to_component_count` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_bom::test_bom_from_schdoc_file` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_bom::test_bom_from_project_file` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_connectivity_review::test_connectivity_is_opt_in_on_file_review` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_project::test_resolves_project_sheets_from_structure` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_project::test_project_review_aggregates_sheets` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_project::test_project_review_accepts_bare_schdoc` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_project::test_design_review_file_disabled_by_default` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_project::test_design_solve_netlist_file_disabled_by_default` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_project::test_design_solve_netlist_file_enabled` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_project::test_design_bom_file_disabled_by_default` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_project::test_design_bom_file_enabled` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_project::test_design_review_file_mcp_tool` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_review::test_review_real_fixture_has_no_errors` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_review::test_real_fixture_has_no_duplicate_unique_ids` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_review::test_annotated_fixture_has_no_unannotated_findings` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_review::test_real_fixture_has_no_malformed_values` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_review::test_review_report_includes_document` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_review::test_sarif_structure_valid` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_schdoc::test_records_parse` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_schdoc::test_extracts_all_components` | needs local-only binary fixture main.SchDoc |
| eda-agent | `tests.test_fileio_schdoc::test_designator_to_part_mapping_is_correct` | needs local-only binary fixture main.SchDoc |
| eda-agent | `tests.test_fileio_schdoc::test_component_fields_present` | needs local-only binary fixture main.SchDoc |
| eda-agent | `tests.test_fileio_schdoc::test_component_parameters_extracted` | needs local-only binary fixture main.SchDoc |
| eda-agent | `tests.test_fileio_schdoc::test_altium_value_reference_resolved` | needs local-only binary fixture main.SchDoc |
| eda-agent | `tests.test_fileio_schdoc::test_every_component_has_an_mpn` | needs local-only binary fixture main.SchDoc |
| eda-agent | `tests.test_fileio_schdoc::test_extracts_declared_nets` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_schdoc::test_extracts_document_info` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_schdoc::test_extracts_wire_segments` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_schdoc::test_extracts_pins_with_consistent_owners` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_schlib::test_reads_library_component_headers` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_fileio_schlib::test_library_review_flags_placeholder_and_missing_desc` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_locale_decimal::test_floats_round_trip_under_a_comma_locale` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_locale_decimal::test_a_junk_string_returns_the_default_instead_of_raising` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_netlist_cache_staleness::test_pascal_and_python_agree_on_the_classifier` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_pascal_runner.TestPascalLogicProgram::test_runs_clean` | Free Pascal Compiler (fpc) not on PATH |
| eda-agent | `tests.test_pascal_runner.TestRealPascalProgram::test_runs_clean` | Free Pascal Compiler (fpc) not on PATH |
| eda-agent | `tests.test_pin_connection_point::test_pascal_agrees_with_the_mirror` | Free Pascal Compiler (fpc) is not installed or not on PATH |
| eda-agent | `tests.test_review_cli::test_review_disabled_by_default_exit_two` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_review_cli::test_review_env_var_enables` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_review_cli::test_review_fixture_exit_zero` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_review_cli::test_review_json_mode` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_review_cli::test_fail_on_threshold` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
| eda-agent | `tests.test_review_cli::test_review_subcommand_registered` | needs a local-only binary Altium fixture not committed to the repo (main.SchDoc, EDAAgentTest.PcbDoc, EDAAgentTest_ICs.SchLib, EDAAgentTest_Export.OutJob) |
