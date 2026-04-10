# portal/views.py
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib import messages
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.cache import never_cache
from django.http import HttpResponse
from django.template.loader import get_template
from django.utils import timezone
from decimal import Decimal
from io import BytesIO

import weasyprint

from .models import (
    StudentProfile, Department, Course, AcademicSession, Semester,
    Result, Fee, FeePayment, GPAResult, CourseRegistration,
    SEMESTER_CHOICES,
)
from .forms import StudentRegistrationForm, StudentProfileForm, FeePaymentForm


# Helpers
def _get_profile_or_none(request):
    """Return the student profile or None if not found."""
    try:
        return request.user.student_profile
    except StudentProfile.DoesNotExist:
        return None


def _render_to_pdf(template_src, context_dict, request=None):
    """Render an HTML template to a PDF byte string using WeasyPrint."""
    template = get_template(template_src)
    html_string = template.render(context_dict, request)
    pdf_bytes = weasyprint.HTML(string=html_string).write_pdf()
    return pdf_bytes



# Public views
def home(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    return redirect('login')


@never_cache
@require_http_methods(['GET', 'POST'])
def register_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        form = StudentRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.first_name = form.cleaned_data['first_name']
            user.last_name  = form.cleaned_data['last_name']
            user.email      = form.cleaned_data['email']
            user.save()

            current_session  = AcademicSession.objects.filter(is_current=True).first()
            current_semester = Semester.objects.filter(is_current=True).first()

            matric = form.cleaned_data['matric_number']
            StudentProfile.objects.create(
                user=user,
                matric_number=matric,
                department=form.cleaned_data['department'],
                current_session=current_session,
                current_semester=current_semester.semester if current_semester else 'First',
                entry_year=matric[:2] if len(matric) >= 2 else '',
            )
            messages.success(request, 'Registration successful! Please log in.')
            return redirect('login')
    else:
        form = StudentRegistrationForm()

    return render(request, 'registration/register.html', {'form': form})


@never_cache
@require_http_methods(['GET', 'POST'])
def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            next_url = request.GET.get('next', '')
            if next_url and next_url.startswith('/') and not next_url.startswith('//'):
                return redirect(next_url)
            return redirect('dashboard')
        messages.error(request, 'Invalid username or password. Please try again.')
    else:
        form = AuthenticationForm()

    return render(request, 'registration/login.html', {'form': form})


@require_POST
def logout_view(request):
    """Logout only via POST to prevent CSRF logout attacks."""
    logout(request)
    messages.success(request, 'You have been signed out successfully.')
    return redirect('login')



# Student — profile setup
@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
def complete_profile(request):
    profile = _get_profile_or_none(request)
    if not profile:
        messages.error(request, 'Student profile not found. Please contact admin.')
        return redirect('login')

    if profile.profile_completed:
        messages.warning(
            request,
            'Your profile has already been saved and cannot be edited. '
            'Contact the admin for any corrections.'
        )
        return redirect('dashboard')

    if request.method == 'POST':
        form = StudentProfileForm(
            request.POST, request.FILES,
            instance=profile, user=request.user
        )
        if form.is_valid():
            form.save()
            messages.success(request, 'Profile saved successfully! Welcome to UniPortal.')
            return redirect('dashboard')
    else:
        form = StudentProfileForm(instance=profile, user=request.user)

    return render(request, 'portal/complete_profile.html', {
        'form': form,
        'profile': profile,
    })


# Student — main dashboard
@never_cache
@login_required
def dashboard(request):
    # Admins go straight to the admin panel
    if request.user.is_staff:
        return redirect('/admin/')

    profile = _get_profile_or_none(request)
    if not profile:
        messages.error(request, 'Student profile not found. Please contact admin.')
        return redirect('login')

    if not profile.profile_completed:
        return redirect('complete_profile')

    current_session  = AcademicSession.objects.filter(is_current=True).first()
    current_semester = Semester.objects.filter(is_current=True).first()

    #  Current semester courses 
    current_courses = Course.objects.filter(
        department=profile.department,
        level=profile.current_level,
        semester=profile.current_semester,
        is_active=True,
    ).order_by('code')

    current_results = Result.objects.filter(
        student=profile,
        course__level=profile.current_level,
        course__semester=profile.current_semester,
    ).select_related('course')

    current_results_dict = {r.course_id: r for r in current_results}
    current_course_data = [
        {'course': c, 'result': current_results_dict.get(c.id)}
        for c in current_courses
    ]

    # Current semester GPA (from stored GPAResult)
    current_gpa = None
    if current_session and current_semester:
        gpa_result = GPAResult.objects.filter(
            student=profile,
            session=current_session,
            semester=current_semester,
        ).first()
        if gpa_result:
            current_gpa = gpa_result.gpa

    #  Past semesters 
    past_semesters = []
    reached_current = False

    for level in profile.department.get_levels():
        if reached_current:
            break
        for sem in ['First', 'Second']:
            if level == profile.current_level and sem == profile.current_semester:
                reached_current = True
                break

            courses = Course.objects.filter(
                department=profile.department,
                level=level,
                semester=sem,
                is_active=True,
            ).order_by('code')

            results = Result.objects.filter(
                student=profile,
                course__level=level,
                course__semester=sem,
            ).select_related('course')

            fee = Fee.objects.filter(
                department=profile.department,
                level=level,
                semester=sem,
            ).first()

            fee_payment = (
                FeePayment.objects.filter(student=profile, fee=fee).first()
                if fee else None
            )

            result_dict = {r.course_id: r for r in results}
            course_data = [
                {'course': c, 'result': result_dict.get(c.id)}
                for c in courses
            ]

            avg = None
            if results.exists():
                total = sum(float(r.total_score) for r in results)
                avg = round(total / results.count(), 2)

            semester_display = dict(SEMESTER_CHOICES).get(sem, sem)

            past_semesters.append({
                'level': level,
                'semester': sem,
                'semester_display': semester_display,
                'courses': course_data,
                'average': avg,
                'fee': fee,
                'fee_payment': fee_payment,
                'has_results': results.exists(),
            })

    #  Current semester fee 
    current_fee = Fee.objects.filter(
        department=profile.department,
        level=profile.current_level,
        semester=profile.current_semester,
    ).first()

    current_fee_payment = (
        FeePayment.objects.filter(student=profile, fee=current_fee).first()
        if current_fee else None
    )

    # Recalculate CGPA
    profile.calculate_cgpa()

    return render(request, 'portal/dashboard.html', {
        'profile': profile,
        'past_semesters': past_semesters,
        'current_courses': current_course_data,
        'current_fee': current_fee,
        'current_fee_payment': current_fee_payment,
        'current_gpa': current_gpa,
        'classification': profile.get_classification(),
    })



# Student — fee receipt upload
@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
def upload_fee_receipt(request, fee_id):
    profile = _get_profile_or_none(request)
    if not profile:
        messages.error(request, 'Student profile not found.')
        return redirect('login')

    fee = get_object_or_404(Fee, id=fee_id, department=profile.department)
    existing_payment = FeePayment.objects.filter(student=profile, fee=fee).first()

    if existing_payment and existing_payment.status == 'paid':
        messages.info(request, 'This fee has already been verified as paid.')
        return redirect('dashboard')

    if request.method == 'POST':
        form = FeePaymentForm(
            request.POST, request.FILES,
            fee=fee, student=profile,
        )
        if form.is_valid():
            if existing_payment:
                existing_payment.delete()

            payment = form.save(commit=False)
            payment.student = profile
            payment.fee     = fee
            payment.status  = 'pending'
            payment.save()
            messages.success(
                request,
                'Payment receipt submitted successfully! '
                'It will be verified by the school admin.'
            )
            return redirect('dashboard')
    else:
        form = FeePaymentForm(fee=fee, student=profile)

    return render(request, 'portal/upload_receipt.html', {
        'form': form,
        'fee': fee,
        'profile': profile,
        'existing_payment': existing_payment,
    })


# Student — semester detail
@login_required
def semester_detail(request, level, semester):
    profile = _get_profile_or_none(request)
    if not profile:
        return redirect('login')

    valid_levels    = ['100', '200', '300', '400', '500']
    valid_semesters = ['First', 'Second']
    if level not in valid_levels or semester not in valid_semesters:
        messages.error(request, 'Invalid semester reference.')
        return redirect('dashboard')

    courses = Course.objects.filter(
        department=profile.department,
        level=level,
        semester=semester,
        is_active=True,
    ).order_by('code')

    results = Result.objects.filter(
        student=profile,
        course__level=level,
        course__semester=semester,
    ).select_related('course', 'session')

    result_dict = {r.course_id: r for r in results}
    course_data = [
        {'course': c, 'result': result_dict.get(c.id)}
        for c in courses
    ]

    avg = None
    if results.exists():
        total = sum(float(r.total_score) for r in results)
        avg   = round(total / results.count(), 2)

    fee = Fee.objects.filter(
        department=profile.department,
        level=level,
        semester=semester,
    ).first()

    fee_payment = (
        FeePayment.objects.filter(student=profile, fee=fee).first()
        if fee else None
    )

    semester_labels = {'First': 'First Semester', 'Second': 'Second Semester'}

    return render(request, 'portal/semester_detail.html', {
        'profile': profile,
        'level': level,
        'semester': semester,
        'semester_display': semester_labels.get(semester, semester),
        'courses': course_data,
        'average': avg,
        'fee': fee,
        'fee_payment': fee_payment,
    })



# PDF Generation — WeasyPrint
@login_required
def result_slip_pdf(request, level, semester):
    profile = _get_profile_or_none(request)
    if not profile:
        return redirect('login')

    valid_levels    = ['100', '200', '300', '400', '500']
    valid_semesters = ['First', 'Second']
    if level not in valid_levels or semester not in valid_semesters:
        messages.error(request, 'Invalid semester reference.')
        return redirect('dashboard')

    courses = Course.objects.filter(
        department=profile.department,
        level=level,
        semester=semester,
        is_active=True,
    ).order_by('code')

    results = Result.objects.filter(
        student=profile,
        course__level=level,
        course__semester=semester,
        status='published',
    ).select_related('course')

    result_dict = {r.course_id: r for r in results}
    course_data = [
        {'course': c, 'result': result_dict.get(c.id)}
        for c in courses
    ]

    total_credits = sum(item['course'].credit_units for item in course_data)

    total_grade_points = Decimal('0.00')
    for item in course_data:
        if item['result']:
            total_grade_points += (
                item['result'].grade_point * item['result'].course.credit_units
            )

    gpa = (
        (total_grade_points / Decimal(str(total_credits))).quantize(Decimal('0.01'))
        if total_credits > 0
        else Decimal('0.00')
    )

    semester_labels = {'First': 'First Semester', 'Second': 'Second Semester'}

    context = {
        'profile': profile,
        'level': level,
        'semester': semester_labels.get(semester, semester),
        'courses': course_data,
        'total_credits': total_credits,
        'gpa': gpa,
        'cgpa': profile.cgpa,
        'generated_date': timezone.now(),
    }

    try:
        pdf_bytes = _render_to_pdf('portal/result_slip_pdf.html', context, request)
        filename  = f"result_slip_{profile.matric_number}_{level}L_{semester}.pdf"
        response  = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response
    except Exception:
        messages.error(request, 'Error generating PDF. Please try again.')
        return redirect('dashboard')


@login_required
def transcript_pdf(request):
    profile = _get_profile_or_none(request)
    if not profile:
        return redirect('login')

    results = Result.objects.filter(
        student=profile,
        status='published',
    ).select_related('course', 'session', 'semester').order_by(
        'session__name', 'semester__semester', 'course__level'
    )

    transcript_data = {}
    for result in results:
        key = (
            f"{result.session.name} — "
            f"{result.course.get_semester_display()} "
            f"({result.course.level}L)"
        )
        transcript_data.setdefault(key, []).append(result)

    context = {
        'profile': profile,
        'transcript_data': transcript_data,
        'cgpa': profile.cgpa,
        'classification': profile.get_classification(),
        'generated_date': timezone.now(),
    }

    try:
        pdf_bytes = _render_to_pdf('portal/transcript_pdf.html', context, request)
        filename  = f"transcript_{profile.matric_number}.pdf"
        response  = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response
    except Exception:
        messages.error(request, 'Error generating PDF. Please try again.')
        return redirect('dashboard')
