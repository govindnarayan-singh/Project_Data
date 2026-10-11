from django import forms

from apps.identity_access.models import AccessRole
from apps.identity_access.permissions import primary_access_role


class VersionedActionForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class AssignmentForm(VersionedActionForm):
    assignee_id = forms.ChoiceField(label="Assign to officer")

    def __init__(self, *args, officers=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assignee_id"].choices = [
            (officer.pk, officer.get_full_name() or officer.get_username()) for officer in officers
        ]


class WorkItemUploadForm(VersionedActionForm):
    document = forms.FileField(
        label="Government PDF",
        widget=forms.ClearableFileInput(attrs={"accept": "application/pdf,.pdf"}),
    )
    claimed_identifier = forms.CharField(max_length=255, required=False, label="Claimed GR/document identifier")
    claimed_source_url = forms.URLField(max_length=1000, required=False, label="Claimed official source URL")
    source_context = forms.CharField(
        max_length=1000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Describe the claimed source. This does not independently verify it.",
    )
    submission_reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None and primary_access_role(user) == AccessRole.IAS_OFFICER:
            self.fields["ias_source_attested"] = forms.BooleanField(
                required=False,
                label="I attest that this copy came from an official government source.",
            )


class LocalCatalogSubmissionForm(VersionedActionForm):
    catalog_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    submission_reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 3}))
    ias_source_attested = forms.BooleanField(required=False)


class ResolutionProposalForm(VersionedActionForm):
    target_document_id = forms.ChoiceField(label="Corpus document")
    reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, documents=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_document_id"].choices = [
            (row["id"], f"{row.get('gr_number') or row['file_name']} — {row.get('department') or 'Department unavailable'}")
            for row in documents
        ]


class ResolutionDecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=(("APPROVE", "Approve resolution"), ("REJECT", "Reject proposal")))
    reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 3}))
    expected_case_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_subject_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class CloseInvalidForm(VersionedActionForm):
    reason = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 3}))
