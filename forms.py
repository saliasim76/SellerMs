from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed
from wtforms import StringField, SelectField, BooleanField, PasswordField, DateField
from wtforms.validators import DataRequired, Email, Length, Optional

class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(3, 80)])
    password = PasswordField('Password', validators=[DataRequired()])
    organization = StringField('Organization', validators=[Optional(), Length(max=100)])
    remember_me = BooleanField('Remember Me')

class TwoFactorForm(FlaskForm):
    code = StringField('Authentication Code', validators=[DataRequired(), Length(6, 6)])

class OwnerForm(FlaskForm):
    # Basic
    name = StringField('Name* / الاسم*', validators=[DataRequired(), Length(2, 200)])
    vat_number = StringField('VAT Number* / رقم ضريبة القيمة المضافة*', validators=[DataRequired(), Length(max=100)])
    crn = StringField('CRN* / رقم السجل التجاري*', validators=[DataRequired(), Length(max=100)])
    phone = StringField('Phone / الهاتف', validators=[Optional(), Length(max=30)])
    fax = StringField('Fax / الفاكس', validators=[Optional(), Length(max=30)])
    email = StringField('Email / البريد الإلكتروني', validators=[Optional(), Email(), Length(max=150)])
    website = StringField('Website / الموقع الإلكتروني', validators=[Optional(), Length(max=200)])
    report_color = StringField('Report Heading Color Code / رمز لون عنوان التقرير*', validators=[Optional()])
    logo = FileField('Browse Logo / تصفح الشعار', validators=[FileAllowed(['jpg','jpeg','png'], 'Images only!')])
    bg_logo = FileField('Browse Background Logo / تصفح الشعار الخلفي', validators=[FileAllowed(['jpg','jpeg','png'], 'Images only!')])
    # Address
    street_name = StringField('Street Name / اسم الشارع', validators=[Optional(), Length(max=250)])
    building_number = StringField('Building Number / رقم المبنى', validators=[Optional(), Length(max=50)])
    additional_number = StringField('Additional Number / رقم إضافي', validators=[Optional(), Length(max=50)])
    district = StringField('District / المنطقة', validators=[Optional(), Length(max=100)])
    city = StringField('City / المدينة', validators=[Optional(), Length(max=100)])
    postal_code = StringField('Postal Code / الرمز البريدي', validators=[Optional(), Length(max=20)])
    country = StringField('Country / الدولة', validators=[Optional(), Length(max=100)])

class BankForm(FlaskForm):
    bank_name = StringField('Bank Name / اسم البنك', validators=[DataRequired(), Length(max=150)])
    account_number = StringField('Account Number / رقم الحساب', validators=[Optional(), Length(max=50)])
    branch = StringField('Branch / الفرع', validators=[Optional(), Length(max=100)])
    swift_code = StringField('SWIFT Code', validators=[Optional(), Length(max=20)])
    iban = StringField('IBAN', validators=[Optional(), Length(max=50)])
    is_primary = BooleanField('Primary / رئيسي')

class SearchForm(FlaskForm):
    q = StringField('Search', validators=[Optional()])


# ─────────────────────────────────────────────────────────────────
# CHART OF ACCOUNTS forms (Level 1 & Level 2)
# ─────────────────────────────────────────────────────────────────
from wtforms import IntegerField
from wtforms.validators import Regexp


class LevelOneForm(FlaskForm):
    """Create a Level 1 account. code_length is forced to 1 server-side."""
    code = StringField(
        'Code',
        validators=[DataRequired(), Length(min=1, max=1),
                    Regexp(r'^[A-Za-z]$', message='Code must be a single letter.')],
    )
    drawers     = StringField('Drawers', validators=[DataRequired(), Length(max=100)])
    drawers_ar  = StringField('Drawers (AR)', validators=[Optional(), Length(max=100)])
    description = StringField('Description', validators=[DataRequired(), Length(max=255)])
    description_ar = StringField('Description (AR)', validators=[Optional(), Length(max=255)])


class LevelOneEditForm(FlaskForm):
    """Edit a Level 1 account — code is fixed, so it is NOT part of this form."""
    drawers     = StringField('Drawers', validators=[DataRequired(), Length(max=100)])
    drawers_ar  = StringField('Drawers (AR)', validators=[Optional(), Length(max=100)])
    description = StringField('Description', validators=[DataRequired(), Length(max=255)])
    description_ar = StringField('Description (AR)', validators=[Optional(), Length(max=255)])


class LevelTwoForm(FlaskForm):
    """Create a Level 2 account. code is auto-generated; description is fixed."""
    level_one_id = SelectField('Level One', coerce=int, validators=[DataRequired()])
    drawers      = StringField('Drawers', validators=[DataRequired(), Length(max=150)])
    drawers_ar   = StringField('Drawers (AR)', validators=[Optional(), Length(max=150)])
    description_ar = StringField('Description (AR)', validators=[Optional(), Length(max=255)])


class LevelTwoEditForm(FlaskForm):
    """Edit a Level 2 account — only drawers is editable."""
    drawers = StringField('Drawers', validators=[DataRequired(), Length(max=150)])
    drawers_ar = StringField('Drawers (AR)', validators=[Optional(), Length(max=150)])
    description_ar = StringField('Description (AR)', validators=[Optional(), Length(max=255)])


# ─────────────────────────────────────────────────────────────────
# CHART OF ACCOUNTS forms — Levels 3, 4, 5
# code / code_length / description are all system-generated and are
# deliberately absent from these forms.
# ─────────────────────────────────────────────────────────────────
class LevelThreeForm(FlaskForm):
    level_two_id = SelectField('Level Two', coerce=int, validators=[DataRequired()])
    drawers      = StringField('Drawers', validators=[DataRequired(), Length(max=200)])
    drawers_ar   = StringField('Drawers (AR)', validators=[Optional(), Length(max=200)])
    description_ar = StringField('Description (AR)', validators=[Optional(), Length(max=255)])


class LevelThreeEditForm(FlaskForm):
    drawers = StringField('Drawers', validators=[DataRequired(), Length(max=200)])
    drawers_ar = StringField('Drawers (AR)', validators=[Optional(), Length(max=200)])
    description_ar = StringField('Description (AR)', validators=[Optional(), Length(max=255)])


class LevelFourForm(FlaskForm):
    level_three_id = SelectField('Level Three', coerce=int, validators=[DataRequired()])
    drawers        = StringField('Drawers', validators=[DataRequired(), Length(max=200)])
    drawers_ar     = StringField('Drawers (AR)', validators=[Optional(), Length(max=200)])
    description_ar = StringField('Description (AR)', validators=[Optional(), Length(max=255)])


class LevelFourEditForm(FlaskForm):
    drawers = StringField('Drawers', validators=[DataRequired(), Length(max=200)])
    drawers_ar = StringField('Drawers (AR)', validators=[Optional(), Length(max=200)])
    description_ar = StringField('Description (AR)', validators=[Optional(), Length(max=255)])


class LevelFiveForm(FlaskForm):
    level_four_id = SelectField('Level Four', coerce=int, validators=[DataRequired()])
    drawers       = StringField('Drawers', validators=[DataRequired(), Length(max=250)])
    drawers_ar    = StringField('Drawers (AR)', validators=[Optional(), Length(max=250)])
    description_ar = StringField('Description (AR)', validators=[Optional(), Length(max=255)])
    control_account = SelectField('Control Account', choices=[('No','No'),('Yes','Yes')], default='No', validators=[Optional()])


class LevelFiveEditForm(FlaskForm):
    drawers = StringField('Drawers', validators=[DataRequired(), Length(max=250)])
    drawers_ar = StringField('Drawers (AR)', validators=[Optional(), Length(max=250)])
    description_ar = StringField('Description (AR)', validators=[Optional(), Length(max=255)])
    control_account = SelectField('Control Account', choices=[('No','No'),('Yes','Yes')], default='No', validators=[Optional()])