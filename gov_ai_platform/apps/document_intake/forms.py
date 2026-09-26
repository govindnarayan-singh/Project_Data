from django import forms

from apps.identity_access.models import OrganizationUnit

from .models import SourceProvenance


class DocumentSubmissionForm(forms.Form):
    document = forms.FileField(
        label="Government PDF",
        widget=forms.ClearableFileInput(attrs={"accept": "application/pdf,.pdf"}),
    )
    organization = forms.ModelChoiceField(queryset=OrganizationUnit.objects.none(), label="Organization")
    claimed_identifier = forms.CharField(max_length=255, required=False, label="Claimed GR/document identifier")
    claimed_source_url = forms.URLField(max_length=1000, required=False, label="Claimed official source URL")
    source_provenance = forms.ChoiceField(
        choices=(
            (SourceProvenance.OFFICER_PROVIDED_UNVERIFIED, "Officer-provided, not independently verified"),
            (SourceProvenance.IAS_VERIFIED_COPY, "IAS-verified copy (not independent source verification)"),
            (SourceProvenance.UNKNOWN, "Unknown provenance"),
        )
    )
    submission_reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 4}))

    def __init__(self, *args, organizations=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organizations is not None:
            self.fields["organization"].queryset = organizations


class DocumentDecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=(("APPROVE", "Approve for corpus admission"), ("REJECT", "Reject")))
    reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 3}))
    expected_case_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_subject_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class RetryIngestionForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
