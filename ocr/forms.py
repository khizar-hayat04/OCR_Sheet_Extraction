from django import forms

from .models import ExtractedCell
from .services.validators import validate_cell_value
from .utils.image_preprocessing import validate_uploaded_image


class SheetUploadForm(forms.Form):
    image = forms.ImageField(label="Upload handwritten sheet")

    def clean_image(self):
        image = self.cleaned_data["image"]
        validate_uploaded_image(image)
        return image


class ConfirmSheetForm(forms.Form):
    def __init__(self, *args, cells=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cells = list(cells or [])
        for cell in self.cells:
            self.fields[self.field_name(cell)] = forms.CharField(
                required=False,
                max_length=32,
                initial=cell.review_value,
            )

    @staticmethod
    def field_name(cell):
        return f"cell_{cell.pk}"

    def clean(self):
        cleaned = super().clean()
        for cell in self.cells:
            value = cleaned.get(self.field_name(cell), "")
            try:
                cleaned[self.field_name(cell)] = validate_cell_value(cell.field_type, value)
            except ValueError as exc:
                self.add_error(self.field_name(cell), str(exc))
        return cleaned

    def save(self):
        for cell in self.cells:
            cell.corrected_value = self.cleaned_data[self.field_name(cell)]
            cell.is_confirmed = True
            cell.save(update_fields=["corrected_value", "is_confirmed"])
