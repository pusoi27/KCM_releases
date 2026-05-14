# routes/qr.py
import os
import io
import sqlite3
from flask import render_template, request, jsonify, send_file, flash, redirect, url_for
from modules import student_manager, assistant_manager, qr_generator, auth_manager, book_manager, materials_manager
from modules.database import DB_PATH
from routes.auth import require_login, require_feature
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode import code128


def _generate_missing_student_qrs():
    """Generate QR codes only for students missing them in database.

    Returns (generated, skipped, errors) where each item is a list.
    """
    students = student_manager.get_all_students()
    generated = []
    skipped = []
    errors = []
    for s in students:
        try:
            sid = s[0]
            # Check if QR code exists in database
            existing_qr = student_manager.get_student_qr_code(sid)
            if existing_qr:
                skipped.append(f"student_{sid}.png")
                continue
            qr_data = f"ID:{sid}\nName:{s[1]}"
            qr_blob = qr_generator.generate_qr_bytes(qr_data)
            student_manager.set_student_qr_code(sid, qr_blob)
            generated.append(f"student_{sid}.png")
        except Exception as e:
            errors.append({'id': sid, 'error': str(e)})
    return generated, skipped, errors

def register_qr_routes(app):
    """Register QR code generation and printing routes."""
    
    # ================================================================
    # QR Code Generation - Students
    # ================================================================
    
    @app.route("/qr/generate")
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_generate():
        """Generate QR code for a specific student."""
        students = student_manager.get_all_students()
        return render_template("qr_generate.html", students=students)

    @app.route("/qr/generate/<int:sid>", methods=["POST", "GET"])
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_generate_student(sid):
        """Generate and return QR code PNG for a student from database."""
        student = student_manager.get_student(sid)
        if not student:
            return "Student not found", 404
        qr_data = f"ID:{student[0]}\nName:{student[1]}"
        qr_blob = qr_generator.generate_qr_bytes(qr_data)
        student_manager.set_student_qr_code(sid, qr_blob)
        return send_file(io.BytesIO(qr_blob), mimetype='image/png')

    @app.route("/qr/generate_all", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_generate_all():
        """Generate QR codes for all students where missing."""
        generated, skipped, errors = _generate_missing_student_qrs()
        return jsonify({'generated': generated, 'skipped': skipped, 'errors': errors, 'generated_count': len(generated), 'skipped_count': len(skipped)})

    @app.route("/qr/students/generate_all", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_STUDENT_DATABASE)
    def qr_students_generate_all_dashboard():
        """Generate missing student QRs from dashboard action and redirect with flash message."""
        generated, skipped, errors = _generate_missing_student_qrs()

        if errors:
            flash(
                f"Generated {len(generated)} QR code(s), skipped {len(skipped)}, with {len(errors)} error(s).",
                "warning",
            )
        elif generated:
            flash(
                f"Generated {len(generated)} QR code(s) for students without assigned QR codes.",
                "success",
            )
        else:
            flash("No QR codes generated; all students have assigned QR codes", "info")

        return redirect(url_for("students_list"))

    @app.route('/students/qr/<int:sid>')
    def serve_student_qr(sid):
        """Serve student QR code from database."""
        qr_blob = student_manager.get_student_qr_code(sid)
        if not qr_blob:
            return "QR code not found", 404
        return send_file(io.BytesIO(qr_blob), mimetype='image/png')

    @app.route('/staff/qr/<int:aid>')
    def serve_staff_qr(aid):
        """Serve staff QR code from database."""
        qr_blob = assistant_manager.get_assistant_qr_code(aid)
        if not qr_blob:
            return "QR code not found", 404
        return send_file(io.BytesIO(qr_blob), mimetype='image/png')

    # ================================================================
    # QR Code Generation - Assistants
    # ================================================================

    @app.route("/qr/assistants/generate_all", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_assistants_generate_all():
        """Generate QR codes for all assistants where missing and store in database."""
        assistants = assistant_manager.get_all_assistants()
        generated, skipped, errors = [], [], []
        for a in assistants:
            aid = a[0]
            name = a[1]
            try:
                # Check if QR code exists in database
                existing_qr = assistant_manager.get_assistant_qr_code(aid)
                if existing_qr:
                    skipped.append(f"assistant_{aid}.png")
                    continue
                qr_data = f"ASST:{aid}\nName:{name}"
                qr_blob = qr_generator.generate_qr_bytes(qr_data)
                assistant_manager.set_assistant_qr_code(aid, qr_blob)
                generated.append(f"assistant_{aid}.png")
            except Exception as e:
                errors.append({'id': aid, 'error': str(e)})

        if errors:
            flash(
                f"Generated {len(generated)} QR code(s), skipped {len(skipped)}, with {len(errors)} error(s).",
                "warning",
            )
        elif generated:
            flash(
                f"Generated {len(generated)} QR code(s) for staff without assigned QR codes.",
                "success",
            )
        else:
            flash("No QR codes generated; all staff has assigned QR codes", "info")

        return redirect(url_for("assistants_list"))

    @app.route("/qr/assistants/generate/<int:aid>", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_assistant_generate(aid):
        """Generate QR code for a single assistant and store in database."""
        assistant = assistant_manager.get_assistant(aid)
        if not assistant:
            return "Staff member not found", 404
        
        existing_qr = assistant_manager.get_assistant_qr_code(aid)
        if existing_qr:
            return jsonify({'message': 'exists', 'file': f'assistant_{aid}.png'})
        
        qr_data = f"ASST:{aid}\nName:{assistant[1]}"
        qr_blob = qr_generator.generate_qr_bytes(qr_data)
        assistant_manager.set_assistant_qr_code(aid, qr_blob)
        return jsonify({'message': 'generated', 'file': f'assistant_{aid}.png'})

    # ================================================================
    # QR Code Generation - Books
    # ================================================================

    @app.route("/qr/books/generate_all", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_books_generate_all():
        """Generate QR codes for all books where missing and store in database."""
        generated, skipped, errors = [], [], []
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, title FROM books")
            for (bid, title) in c.fetchall():
                try:
                    # Check if QR code exists in database
                    existing_qr = book_manager.get_book_qr_code(bid)
                    if existing_qr:
                        skipped.append(f"book_{bid}.png")
                        continue
                    qr_data = f"BOOK:{bid}\nTitle:{title or ''}"
                    qr_blob = qr_generator.generate_qr_bytes(qr_data)
                    book_manager.set_book_qr_code(bid, qr_blob)
                    generated.append(f"book_{bid}.png")
                except Exception as e:
                    errors.append({'id': bid, 'error': str(e)})
        return jsonify({'generated': generated, 'skipped': skipped, 'errors': errors})

    @app.route("/qr/books/generate/<int:bid>", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_book_generate(bid):
        """Generate QR code for a single book and store in database."""
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            row = c.execute("SELECT id, title FROM books WHERE id=?", (bid,)).fetchone()
        if not row:
            return jsonify({"error": "Book not found"}), 404
        
        existing_qr = book_manager.get_book_qr_code(bid)
        if existing_qr:
            return jsonify({'message': 'exists', 'file': f'book_{bid}.png'})
        
        qr_data = f"BOOK:{bid}\nTitle:{row[1] or ''}"
        qr_blob = qr_generator.generate_qr_bytes(qr_data)
        book_manager.set_book_qr_code(bid, qr_blob)
        return jsonify({'message': 'generated', 'file': f'book_{bid}.png'})

    # ================================================================
    # QR Code PDF Generation
    # ================================================================

    @app.route('/qr/pdf/individual/<int:sid>')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_pdf_individual(sid):
        """Generate Avery 8160 PDF for a single student."""
        student = student_manager.get_student(sid)
        if not student:
            return "Student not found", 404
        
        qr_blob = student_manager.get_student_qr_code(sid)
        if not qr_blob:
            # Generate and store if missing
            try:
                qr_data = f"ID:{sid}\nName:{student[1]}"
                qr_blob = qr_generator.generate_qr_bytes(qr_data)
                student_manager.set_student_qr_code(sid, qr_blob)
            except Exception:
                qr_blob = None
        
        # Convert bytes to BytesIO for PDF rendering
        qr_io = io.BytesIO(qr_blob) if qr_blob else None
        labels = [{'name': student[1], 'qr_blob': qr_io}]
        buf = _build_avery_pdf(labels)
        filename = f'student_{sid}_labels.pdf'
        return send_file(buf, mimetype='application/pdf', as_attachment=True, download_name=filename)

    @app.route('/qr/pdf/all')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_pdf_all():
        """Generate Avery 8160 PDF for all students."""
        students = student_manager.get_all_students()
        labels = []
        for s in students:
            sid = s[0]
            qr_blob = student_manager.get_student_qr_code(sid)
            if not qr_blob:
                # Generate and store if missing
                try:
                    qr_data = f"ID:{sid}\nName:{s[1]}"
                    qr_blob = qr_generator.generate_qr_bytes(qr_data)
                    student_manager.set_student_qr_code(sid, qr_blob)
                except Exception:
                    qr_blob = None
            
            qr_io = io.BytesIO(qr_blob) if qr_blob else None
            labels.append({'name': s[1], 'qr_blob': qr_io})
        buf = _build_avery_pdf(labels)
        filename = 'students_qr_labels.pdf'
        return send_file(buf, mimetype='application/pdf', as_attachment=True, download_name=filename)

    @app.route('/qr/assistants/pdf')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_assistants_pdf():
        """Generate Avery 8163 PDF for assistants with existing QR codes."""
        assistants = assistant_manager.get_all_assistants()
        labels = []
        for a in assistants:
            aid = a[0]
            qr_blob = assistant_manager.get_assistant_qr_code(aid)
            if qr_blob:
                qr_io = io.BytesIO(qr_blob)
                labels.append({'name': a[1], 'qr_blob': qr_io})
        if not labels:
            return "No staff QR codes found. Generate them first.", 400
        pdf_buffer = _build_avery8163_pdf(labels)
        return send_file(pdf_buffer, as_attachment=True, download_name="assistant_qr_avery8163.pdf", mimetype='application/pdf')

    @app.route('/qr/assistants/pdf/individual/<int:aid>')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_assistant_pdf_individual(aid):
        """Generate Avery 8163 PDF for a single assistant."""
        assistant = assistant_manager.get_assistant(aid)
        if not assistant:
            return "Staff member not found", 404
        qr_blob = assistant_manager.get_assistant_qr_code(aid)
        if not qr_blob:
            # Generate and store if missing
            try:
                qr_data = f"ASST:{aid}\nName:{assistant[1]}"
                qr_blob = qr_generator.generate_qr_bytes(qr_data)
                assistant_manager.set_assistant_qr_code(aid, qr_blob)
            except Exception:
                return "Failed to generate QR code.", 500
        qr_io = io.BytesIO(qr_blob)
        labels = [{'name': assistant[1], 'qr_blob': qr_io}]
        pdf_buffer = _build_avery8163_pdf(labels)
        return send_file(pdf_buffer, as_attachment=True, download_name=f"assistant_{aid}_qr.pdf", mimetype='application/pdf')

    @app.route('/qr/books/pdf')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_books_pdf():
        """Generate Avery 8163 PDF for all books with existing QR codes."""
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, title FROM books")
            books = c.fetchall()
        labels = []
        for b in books:
            bid = b[0]
            qr_blob = book_manager.get_book_qr_code(bid)
            if qr_blob:
                qr_io = io.BytesIO(qr_blob)
                labels.append({'name': b[1], 'qr_blob': qr_io})
        if not labels:
            return "No book QR codes found. Generate them first.", 400
        pdf_buffer = _build_avery8163_pdf(labels)
        return send_file(pdf_buffer, as_attachment=True, download_name="books_qr_avery8163.pdf", mimetype='application/pdf')

    @app.route('/qr/books/pdf/individual/<int:bid>')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_book_pdf_individual(bid):
        """Generate PDF for a single book QR."""
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            row = c.execute("SELECT id, title FROM books WHERE id=?", (bid,)).fetchone()
        if not row:
            return "Book not found", 404
        qr_blob = book_manager.get_book_qr_code(bid)
        if not qr_blob:
            # Generate and store if missing
            try:
                qr_data = f"BOOK:{bid}\nTitle:{row[1] or ''}"
                qr_blob = qr_generator.generate_qr_bytes(qr_data)
                book_manager.set_book_qr_code(bid, qr_blob)
            except Exception:
                return "Failed to generate QR code.", 500
        qr_io = io.BytesIO(qr_blob)
        labels = [{'name': row[1], 'qr_blob': qr_io}]
        pdf_buffer = _build_avery8163_pdf(labels)
        return send_file(pdf_buffer, as_attachment=True, download_name=f"book_{bid}_qr.pdf", mimetype='application/pdf')

    # ================================================================
    # ISBN Print - Books
    # ================================================================

    @app.route('/isbn/pdf/individual/<int:bid>')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def isbn_pdf_individual(bid):
        """Generate Avery 8160 PDF with ISBN for a single book."""
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            row = c.execute("SELECT id, title, isbn13 FROM books WHERE id=?", (bid)).fetchone()
        if not row:
            return "Book not found", 404
        
        bid, title, isbn13 = row[0], row[1], row[2]
        if not isbn13:
            return "ISBN13 not found for this book", 400
        
        labels = [{'name': title, 'isbn': isbn13}]
        buf = _build_isbn_pdf(labels)
        filename = f'book_{bid}_isbn_labels.pdf'
        return send_file(buf, mimetype='application/pdf', as_attachment=True, download_name=filename)

    @app.route('/isbn/pdf/all')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def isbn_pdf_all():
        """Generate PDF with ISBN labels for all books that have valid ISBN (ISBN-13 or ISBN-10)."""
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, title, isbn13, isbn FROM books ")
            books = c.fetchall()
        
        labels = []
        for b in books:
            # Prefer ISBN13, fallback to ISBN10
            isbn_value = b[2] or b[3]
            if isbn_value:
                labels.append({'name': b[1], 'isbn': isbn_value})
        
        if not labels:
            return "No books with valid ISBN found", 400
        
        buf = _build_isbn_pdf(labels)
        filename = 'books_isbn_labels.pdf'
        return send_file(buf, mimetype='application/pdf', as_attachment=True, download_name=filename)

    # ================================================================
    # QR Code Print Pages
    # ================================================================

    @app.route("/qr/print/individual")
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_print_individual():
        """Page to select a student and print their QR code."""
        students = student_manager.get_all_students()
        return render_template("qr_print_individual.html", students=students)

    @app.route("/qr/print/all")
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_print_all():
        """Generate and display QR codes for all active students."""
        students = student_manager.get_all_students()
        active_students = [s for s in students if len(s) >= 8 and s[7] == 1]
        return render_template("qr_print_all.html", students=active_students)

    @app.route("/qr/generate_page")
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_generate_page():
        """Display unified QR generation page for students, assistants, and books."""
        students = student_manager.get_all_students()
        assistants = assistant_manager.get_all_assistants()
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, title FROM books")
            books = c.fetchall()
        return render_template("qr_generate_all.html", students=students, assistants=assistants, books=books)

    @app.route("/qr/print_page")
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_SETTINGS)
    def qr_print_page():
        """Display unified QR print page for students, assistants, and books."""
        students = student_manager.get_all_students()
        assistants = assistant_manager.get_all_assistants()
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT id, title FROM books")
            books = c.fetchall()
        return render_template("qr_print_all.html", students=students, assistants=assistants, books=books)


def _build_avery_pdf(labels):
    """Build PDF for Avery 8160 (1" x 2.625" labels, 3 columns x 10 rows per page).
    
    Standard 8.5x11 paper layout:
    - 3 columns x 10 rows = 30 labels per page
    - Label size: 1" H x 2.625" W
    - Left/Right margins: 0.3125"
    - Top/Bottom margins: 0.5"
    
    Labels can have 'qr_blob' (BytesIO) or 'qr_path' (file path) for backward compatibility.
    """
    buffer = io.BytesIO()
    page_width, page_height = letter  # 8.5" x 11"
    c = canvas.Canvas(buffer, pagesize=(page_width, page_height))

    cols = 3
    rows = 10
    label_w = 2.625 * inch
    label_h = 1.0 * inch
    left_margin = 0.3125 * inch
    right_margin = 0.3125 * inch
    top_margin = 0.5 * inch
    bottom_margin = 0.5 * inch
    qr_size = 0.8 * inch

    labels_per_page = cols * rows
    total = len(labels)
    pages = (total + labels_per_page - 1) // labels_per_page or 1

    idx = 0
    for p in range(pages):
        for r in range(rows):
            for c_idx in range(cols):
                # Adjust third column to the right by 0.25"
                x_offset = 0.25 * inch if c_idx == 2 else 0
                x = left_margin + c_idx * label_w + x_offset
                y = page_height - top_margin - (r + 1) * label_h
                
                if idx < total:
                    lab = labels[idx]
                    c.rect(x, y, label_w, label_h, stroke=0, fill=0)
                    padding = 0.06 * inch
                    qr_x = x + padding
                    name_x = qr_x + qr_size + (0.08 * inch)
                    name_width = label_w - (qr_size + padding + 0.08 * inch + padding)

                    # Calculate text position first to align QR code with it
                    name = (lab.get('name') or '')
                    font_size = 11
                    c.setFont('Helvetica-Bold', font_size)
                    while c.stringWidth(name, 'Helvetica-Bold', font_size) > name_width and font_size > 5:
                        font_size -= 1
                        c.setFont('Helvetica-Bold', font_size)
                    
                    # Adjust text vertical position based on row
                    base_text_y = y + (label_h - font_size) / 2 - 1
                    if r == 0:
                        # Top row: move up 0.25"
                        text_y = base_text_y + 0.25 * inch
                    elif r == rows - 1:
                        # Bottom row: move down 0.25"
                        text_y = base_text_y - 0.25 * inch
                    else:
                        # Middle rows: distribute adjustment linearly
                        # Interpolate from +0.25" (top) to -0.25" (bottom)
                        adjustment = 0.25 * inch * (1 - (2 * r / (rows - 1)))
                        text_y = base_text_y + adjustment
                    
                    # Center QR code vertically to align with text baseline
                    # text_y is the baseline, so center QR around it
                    qr_y = text_y - (qr_size / 2) + (font_size / 2)

                    # Try qr_blob first (database source), then qr_path (file source for backward compatibility)
                    qr_source = None
                    if lab.get('qr_blob'):
                        qr_source = lab['qr_blob']
                    elif lab.get('qr_path') and os.path.exists(lab['qr_path']):
                        qr_source = lab['qr_path']
                    
                    if qr_source:
                        try:
                            c.drawImage(qr_source, qr_x, qr_y, width=qr_size, height=qr_size, preserveAspectRatio=True, mask='auto')
                        except Exception:
                            pass
                    
                    # Truncate name if it exceeds available width
                    char_width = c.stringWidth('W', 'Helvetica-Bold', font_size)
                    if char_width > 0:
                        max_chars = int(name_width / char_width)
                        if max_chars > 3 and len(name) > max_chars:
                            name = name[:max_chars-3] + '...'
                    c.drawString(name_x, text_y, name)
                else:
                    pass
                idx += 1
        c.showPage()

    c.save()
    buffer.seek(0)
    return buffer


def _format_isbn_human(isbn: str) -> str:
    """Return a human-friendly ISBN string with light grouping."""
    digits = ''.join(ch for ch in (isbn or '') if ch.isdigit())
    if len(digits) == 13:
        return f"{digits[0:4]}-{digits[4:8]}-{digits[8:12]}-{digits[12:13]}"
    return digits


def _build_isbn_pdf(labels):
    """Build PDF for Avery 8160 ISBN labels (1" H x 2.625" W) with minimal spacing.
    
    Standard 8.5x11 paper layout:
    - 3 columns x 10 rows = 30 labels per page
    - Label size: 1" H x 2.625" W
    - Left/Right margins: 0.3125"
    - Top/Bottom margins: 0.5"
    
    Tight layout with no vertical gaps:
    - Title at top (small font, tight)
    - Barcode centered below
    - Human-readable ISBN at bottom (tight)
    """
    if not labels:
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=letter)
        c.save()
        buffer.seek(0)
        return buffer

    buffer = io.BytesIO()
    page_width, page_height = letter
    c = canvas.Canvas(buffer, pagesize=(page_width, page_height))

    cols = 3
    rows = 10
    label_w = 2.625 * inch
    label_h = 1.0 * inch
    left_margin = 0.3125 * inch
    top_margin = 0.5 * inch
    
    margin_v = 0.04 * inch  # tiny vertical margin
    title_h = 0.14 * inch
    barcode_h = 0.6 * inch
    isbn_text_h = 0.1 * inch

    labels_per_page = cols * rows
    total = len(labels)
    pages = (total + labels_per_page - 1) // labels_per_page or 1

    idx = 0
    for p in range(pages):
        for r in range(rows):
            for c_idx in range(cols):
                # Adjust third column to the right by 0.25"
                x_offset = 0.25 * inch if c_idx == 2 else 0
                x = left_margin + c_idx * label_w + x_offset
                y = page_height - top_margin - (r + 1) * label_h
                
                if idx < total:
                    lab = labels[idx]
                    # Draw border
                    c.rect(x, y, label_w, label_h, stroke=1, fill=0)

                    title = (lab.get('name') or '').strip()
                    isbn_raw = lab.get('isbn', '') or ''
                    isbn_digits = ''.join(ch for ch in isbn_raw if ch.isdigit())

                    if isbn_digits:
                        # Positions (no gaps) - barcode.drawOn() uses bottom-left corner as origin
                        y_top = y + label_h - margin_v
                        
                        # Title at the top (text baseline position)
                        title_y = y_top - 0.08 * inch
                        
                        # Barcode below title (bottom of barcode)
                        barcode_bottom_y = y_top - 0.12 * inch - barcode_h
                        
                        # ISBN text at the bottom (text baseline position)
                        isbn_text_y = y + margin_v + 0.04 * inch

                        # Title
                        if title:
                            c.setFont('Helvetica-Bold', 5)
                            max_title_chars = 30
                            display_title = title if len(title) <= max_title_chars else title[:max_title_chars-3] + '...'
                            c.drawCentredString(x + label_w / 2, title_y, display_title)

                        # Barcode
                        human_isbn = _format_isbn_human(isbn_digits)
                        barcode = code128.Code128(
                            isbn_digits,
                            barHeight=barcode_h,
                            barWidth=1.2  # wider bars to spread across label
                        )

                        barcode_width = barcode.width
                        max_barcode_width = label_w - 0.05 * inch  # use nearly full width
                        barcode_x = x + (label_w - min(barcode_width, max_barcode_width)) / 2

                        if barcode_width > max_barcode_width:
                            scale = max_barcode_width / barcode_width
                            c.saveState()
                            c.translate(barcode_x, barcode_bottom_y)
                            c.scale(scale, 1)
                            barcode.drawOn(c, 0, 0)
                            c.restoreState()
                        else:
                            barcode.drawOn(c, barcode_x, barcode_bottom_y)

                        # ISBN text
                        c.setFont('Helvetica', 6)
                        c.drawCentredString(x + label_w / 2, isbn_text_y, human_isbn)

                idx += 1
        c.showPage()

    c.save()
    buffer.seek(0)
    return buffer


def _build_isbn_8163_pdf(labels):
    """Build PDF for Avery 8163 (2" x 4") that contains book title, ISBN-13 barcode, and human-readable ISBN.

    Standard 8.5x11 portrait layout:
    - 2 columns x 5 rows = 10 labels per page
    - Label size: 2" H x 4" W
    - Left/Right margins: 0.5"
    - Top/Bottom margins: 0.5"
    """
    buffer = io.BytesIO()
    page_width, page_height = letter
    c = canvas.Canvas(buffer, pagesize=(page_width, page_height))

    cols = 2
    rows = 5
    label_w = 4.0 * inch
    label_h = 2.0 * inch
    left_margin = 0.5 * inch
    top_margin = 0.5 * inch

    # Layout spacing within label (generous space)
    title_font_size = 11
    title_height = 0.35 * inch
    barcode_height = 0.9 * inch
    isbn_text_height = 0.18 * inch
    spacing = 0.06 * inch
    max_barcode_width = label_w - 0.25 * inch

    labels_per_page = cols * rows
    total = len(labels)
    pages = (total + labels_per_page - 1) // labels_per_page or 1

    idx = 0
    for _ in range(pages):
        for r in range(rows):
            for c_idx in range(cols):
                x = left_margin + c_idx * label_w
                y = page_height - top_margin - (r + 1) * label_h

                if idx < total:
                    lab = labels[idx]
                    # Draw label border
                    c.rect(x, y, label_w, label_h, stroke=1, fill=0)

                    title = (lab.get('name') or '').strip()
                    isbn_raw = lab.get('isbn', '') or ''
                    isbn_digits = ''.join(ch for ch in isbn_raw if ch.isdigit())

                    if isbn_digits:
                        # Vertical positions
                        label_top = y + label_h
                        title_y = label_top - spacing - title_height / 2
                        barcode_y_top = label_top - spacing - title_height - spacing
                        barcode_y = barcode_y_top - barcode_height
                        isbn_text_y = y + spacing + isbn_text_height / 2

                        # Title (allow longer text; truncate if extremely long)
                        if title:
                            c.setFont('Helvetica-Bold', title_font_size)
                            max_title_chars = 60
                            display_title = title if len(title) <= max_title_chars else title[:max_title_chars-3] + '...'
                            c.drawCentredString(x + label_w / 2, title_y, display_title)

                        # Barcode
                        human_isbn = _format_isbn_human(isbn_digits)
                        barcode = code128.Code128(
                            isbn_digits,
                            barHeight=barcode_height,
                            barWidth=0.8  # more width available on 4" label
                        )

                        barcode_width = barcode.width
                        barcode_x = x + (label_w - min(barcode_width, max_barcode_width)) / 2

                        if barcode_width > max_barcode_width:
                            scale = max_barcode_width / barcode_width
                            c.saveState()
                            c.translate(barcode_x, barcode_y)
                            c.scale(scale, 1)
                            barcode.drawOn(c, 0, 0)
                            c.restoreState()
                        else:
                            barcode.drawOn(c, barcode_x, barcode_y)

                        # Human-readable ISBN at bottom
                        c.setFont('Helvetica', 9)
                        c.drawCentredString(x + label_w / 2, isbn_text_y, human_isbn)

                idx += 1
        c.showPage()

    c.save()
    buffer.seek(0)
    return buffer


def _build_avery8163_pdf(labels):
    """Build PDF for Avery 8163 (2" x 4" labels, 2 columns x 5 rows per page).
    
    Labels can have 'qr_blob' (BytesIO) or 'qr_path' (file path) for backward compatibility.
    """
    buffer = io.BytesIO()
    page_width, page_height = letter  # portrait
    c = canvas.Canvas(buffer, pagesize=(page_width, page_height))

    cols = 2
    rows = 5
    label_w = 4.0 * inch
    label_h = 2.0 * inch
    left_margin = 0.5 * inch
    top_margin = 0.5 * inch
    qr_size = 1.4 * inch

    labels_per_page = cols * rows
    total = len(labels)
    pages = (total + labels_per_page - 1) // labels_per_page or 1

    idx = 0
    for _ in range(pages):
        for r in range(rows):
            for col in range(cols):
                if idx >= total:
                    break
                label = labels[idx]
                x = left_margin + col * label_w
                y = page_height - top_margin - (r + 1) * label_h

                c.setStrokeColorRGB(0.85, 0.85, 0.85)
                c.rect(x, y, label_w, label_h, stroke=1, fill=0)

                # Try qr_blob first (database source), then qr_path (file source for backward compatibility)
                qr_source = None
                if label.get('qr_blob'):
                    qr_source = label['qr_blob']
                elif label.get("qr_path") and os.path.exists(label.get("qr_path")):
                    qr_source = label.get("qr_path")
                
                if qr_source:
                    try:
                        c.drawImage(qr_source, x + 0.2 * inch, y + (label_h - qr_size) / 2, qr_size, qr_size, preserveAspectRatio=True)
                    except Exception:
                        pass

                c.setFont("Helvetica-Bold", 14)
                c.drawString(x + qr_size + 0.4 * inch, y + label_h / 2 + 4, label.get("name", ""))
                idx += 1
        c.showPage()

    c.save()
    buffer.seek(0)
    return buffer
