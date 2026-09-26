from django import forms

from apps.identity_access.models import AccessRole
from apps.identity_access.permissions import primary_access_role
from apps.identity_access.models import OrganizationUnit


class DocumentSubmissionForm(forms.Form):
    document = forms.FileField(
        label="Government PDF",
        widget=forms.ClearableFileInput(attrs={"accept": "application/pdf,.pdf"}),
    )
    organization = forms.ModelChoiceField(queryset=OrganizationUnit.objects.none(), label="Organization")
    claimed_identifier = forms.CharField(max_length=255, required=False, label="Claimed GR/document identifier")
    claimed_source_url = forms.URLField(max_length=1000, required=False, label="Claimed official source URL")
    source_context = forms.CharField(
        max_length=1000,
        required=False,
        label="How did you obtain this document?",
        help_text="Optional. Describe the claimed source; this does not verify it.",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    submission_reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 4}))

    def __init__(self, *args, organizations=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organizations is not None:
            self.fields["organization"].queryset = organizations
        self.uploader_role = primary_access_role(user) if user is not None else ""
        if self.uploader_role == AccessRole.IAS_OFFICER:
            self.fields["ias_source_attested"] = forms.BooleanField(
                required=False,
                label="I attest that this copy was obtained from an official government source.",
                help_text="IAS attestation is recorded separately and is not independent source verification.",
            )


class DocumentDecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=(("APPROVE", "Approve for corpus admission"), ("REJECT", "Reject")))
    reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 3}))
    expected_case_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_subject_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class RetryIngestionForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class RevalidateDocumentForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
