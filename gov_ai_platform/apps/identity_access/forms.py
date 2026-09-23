from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError

from .models import AccessGrantState, AccessRequest, OrganizationUnit


class LoginForm(forms.Form):
    identifier = forms.CharField(max_length=254, label="Username or official email")
    password = forms.CharField(max_length=128, widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))
    next = forms.CharField(required=False, widget=forms.HiddenInput)


class AccessRequestForm(forms.ModelForm):
    website = forms.CharField(required=False, widget=forms.HiddenInput, label="Leave blank")

    class Meta:
        model = AccessRequest
        fields = ("full_name", "official_email", "employee_identifier", "organization", "designation", "reason")
        widgets = {
            "reason": forms.Textarea(attrs={"rows": 4}),
            "full_name": forms.TextInput(attrs={"autocomplete": "name"}),
            "official_email": forms.EmailInput(attrs={"autocomplete": "email"}),
        }
        labels = {
            "employee_identifier": "Employee / official identifier",
            "organization": "Department / office / unit",
            "reason": "Reason for access",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        organizations = OrganizationUnit.objects.filter(is_active=True)
        if settings.ACCESS_ALLOWED_ORGANIZATION_CODES:
            organizations = organizations.filter(code__in=settings.ACCESS_ALLOWED_ORGANIZATION_CODES)
        self.fields["organization"].queryset = organizations.order_by("name", "code")

    def clean(self):
        cleaned = super().clean()
        forbidden = {"role", "access_role", "is_staff", "is_superuser", "organization_id"}
        if forbidden.intersection(self.data.keys()):
            raise ValidationError("Unsupported privilege or identity fields were supplied.")
        return cleaned


class RequestStatusForm(forms.Form):
    request_id = forms.UUIDField(label="Request ID")
    status_token = forms.CharField(max_length=100, strip=True, widget=forms.PasswordInput(render_value=True), label="Private status key")


class DecisionForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    decision_reason = forms.CharField(max_length=1000, strip=True, widget=forms.Textarea(attrs={"rows": 3}), label="Decision reason / comment")


class GrantActionForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    new_state = forms.ChoiceField(choices=(
        (AccessGrantState.SUSPENDED, "Suspend"),
        (AccessGrantState.REVOKED, "Revoke"),
    ))
    reason = forms.CharField(max_length=1000, strip=True, widget=forms.Textarea(attrs={"rows": 2}))
