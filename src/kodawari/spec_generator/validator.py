from __future__ import annotations

from .models import Spec, ValidationMessage, ValidationResult


class SpecValidator:
    _REQUIRED_FIELDS = [
        ("spec_id", "missing spec_id"),
        ("prd_clause", "missing prd_clause"),
        ("epic", "missing epic"),
        ("priority", "missing priority"),
    ]
    _SECTION_RULES = [
        ("algorithm", "algorithm", "algorithm spec missing algorithm section", "algorithm"),
        ("data", "data_structure", "data spec missing data_structure section", "data_structure"),
        ("api", "api_contract", "api spec missing api_contract section", "api_contract"),
    ]

    def _append_error(
        self,
        errors: list[ValidationMessage],
        *,
        message: str,
        field: str,
    ) -> None:
        errors.append(ValidationMessage(level="error", message=message, field=field))

    def _append_warning(
        self,
        warnings: list[ValidationMessage],
        *,
        message: str,
        field: str,
    ) -> None:
        warnings.append(ValidationMessage(level="warning", message=message, field=field))

    def _validate_required_fields(self, spec: Spec, errors: list[ValidationMessage]) -> None:
        for field_name, message in self._REQUIRED_FIELDS:
            if getattr(spec, field_name):
                continue
            self._append_error(errors, message=message, field=field_name)
        if not spec.spec_types:
            self._append_error(errors, message="missing spec_types", field="spec_types")

    def _validate_spec_sections(self, spec: Spec, errors: list[ValidationMessage]) -> None:
        for spec_type, attr_name, message, field in self._SECTION_RULES:
            if spec_type not in spec.spec_types:
                continue
            if getattr(spec, attr_name):
                continue
            self._append_error(errors, message=message, field=field)

    def _validate_acceptance_tests(
        self,
        spec: Spec,
        errors: list[ValidationMessage],
        warnings: list[ValidationMessage],
    ) -> None:
        if not spec.acceptance_tests:
            self._append_error(errors, message="missing acceptance_tests", field="acceptance_tests")
            return
        for index, test_case in enumerate(spec.acceptance_tests):
            if not test_case.get("test_name"):
                self._append_error(
                    errors,
                    message="test case missing test_name",
                    field=f"acceptance_tests[{index}].test_name",
                )
            if not test_case.get("assertions"):
                self._append_warning(
                    warnings,
                    message="test case missing assertions",
                    field=f"acceptance_tests[{index}].assertions",
                )

    def _validate_dependencies(
        self,
        spec: Spec,
        errors: list[ValidationMessage],
        warnings: list[ValidationMessage],
    ) -> None:
        for dep_index, dep in enumerate(spec.dependencies):
            if not dep.get("spec_id"):
                self._append_error(
                    errors,
                    message="dependency missing spec_id",
                    field=f"dependencies[{dep_index}].spec_id",
                )
            if not dep.get("reason"):
                self._append_warning(
                    warnings,
                    message="dependency missing reason",
                    field=f"dependencies[{dep_index}].reason",
                )

    def validate_spec(self, spec: Spec) -> ValidationResult:
        errors: list[ValidationMessage] = []
        warnings: list[ValidationMessage] = []

        self._validate_required_fields(spec, errors)
        self._validate_spec_sections(spec, errors)
        self._validate_acceptance_tests(spec, errors, warnings)
        self._validate_dependencies(spec, errors, warnings)

        return ValidationResult(valid=(len(errors) == 0), errors=errors, warnings=warnings)
