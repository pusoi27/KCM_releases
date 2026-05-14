# Staff Profile Pictures - Feature Analysis

**Date:** May 13, 2026  
**Status:** ✓ Database & Backend Ready | ⚠️ UI Not Yet Implemented

---

## Summary

**YES**, pictures CAN be added to staff profiles, but the feature is partially implemented:

- ✅ **Database**: `staff` table has `icon_picture` (BLOB) and `icon_picture_mime` (TEXT) columns
- ✅ **Backend**: Photo storage/retrieval functions exist in `assistant_manager` module
- ⚠️ **UI**: Photo upload form NOT implemented in staff profile editor
- ⚠️ **Display**: Staff photos NOT displayed in staff list or profile views

---

## Current Implementation Status

### Database Schema ✅
The `staff` table already has the necessary fields:

| Field | Type | Purpose |
|-------|------|---------|
| `id` | INTEGER | Staff member ID |
| `name` | TEXT | Staff name |
| `role` | TEXT | Job role |
| `email` | TEXT | Email address |
| `phone` | TEXT | Phone number |
| `qr_code` | BLOB | QR code (existing) |
| **`icon_picture`** | **BLOB** | **Photo image data** |
| **`icon_picture_mime`** | **TEXT** | **Image MIME type (e.g., 'image/png')** |

### Backend Functions ✅
Available in [modules/assistant_manager.py](modules/assistant_manager.py):

**`set_assistant_icon(assistant_id, icon_blob=None, icon_mime='')`**
- Stores staff photo as binary data in database
- Supports any image format with MIME type
- Example MIME types: `'image/jpeg'`, `'image/png'`, `'image/webp'`

**`get_assistant_icon(assistant_id)`**
- Retrieves photo from database
- Returns: `{'icon_blob': bytes, 'icon_mime': 'image/jpeg'}`
- Returns `None` if no photo exists

### UI/Routes ⚠️
The staff profile form ([templates/assistant_form.html](templates/assistant_form.html) and [routes/assistants.py](routes/assistants.py)) currently has:

| Field | Status |
|-------|--------|
| Name | ✅ |
| Role | ✅ |
| Email | ✅ |
| Phone | ✅ |
| **Photo** | **❌ Not implemented** |

---

## Comparison: Students vs. Staff

| Feature | Students | Staff |
|---------|----------|-------|
| Database photo field | ✅ Yes | ✅ Yes |
| Backend functions | ✅ Yes (set_student_photo) | ✅ Yes (set_assistant_icon) |
| Form upload UI | ✅ Implemented | ❌ Not implemented |
| Display in list | ✅ Yes | ❌ No |
| Display in profile | ✅ Yes | ❌ No |

---

## How to Add Staff Photos

To implement staff photo upload, you would need to:

### 1. **Update Staff Form Template** 
Add to [templates/assistant_form.html](templates/assistant_form.html):
```html
<div class="mb-3">
  <label class="form-label">Photo</label>
  <input type="file" name="photo" class="form-control" accept="image/jpeg,image/png,image/webp">
</div>
```

### 2. **Update Routes** 
Modify [routes/assistants.py](routes/assistants.py) to:
- Accept file upload in `POST /assistants/add` and `/assistants/edit/<id>`
- Validate image format and size
- Convert image to BLOB
- Call `assistant_manager.set_assistant_icon()`

### 3. **Display Photos**
Update [templates/assistants.html](templates/assistants.html) and staff profile views to display photos using:
```html
<img src="/api/assistant/<id>/icon" alt="{{ staff.name }}">
```

### 4. **Add API Endpoint** 
Create `/api/assistant/<id>/icon` route that:
- Retrieves photo from database
- Returns appropriate MIME type
- Handles missing photos gracefully

---

## Implementation Example

Here's a minimal code sample showing how to save/retrieve staff photos:

```python
# In routes/assistants.py

@app.route("/assistants/edit/<int:aid>", methods=["POST"])
@require_login
def assistants_edit(aid):
    # ... existing code ...
    
    # Handle photo upload
    photo_file = request.files.get('photo')
    if photo_file and photo_file.filename:
        from PIL import Image
        from io import BytesIO
        
        # Validate and resize image
        try:
            img = Image.open(photo_file)
            img.thumbnail((200, 200))  # Max 200x200
            
            # Convert to BLOB
            img_buffer = BytesIO()
            img.save(img_buffer, format='PNG')
            photo_blob = img_buffer.getvalue()
            
            # Store in database
            assistant_manager.set_assistant_icon(
                aid, 
                icon_blob=photo_blob,
                icon_mime='image/png'
            )
        except Exception as e:
            flash(f"Error processing photo: {e}", "warning")
```

---

## Current Limitations

1. **Students have photos** - Fully implemented and working
2. **Staff photos not exposed** - Database ready but UI/routing not connected
3. **QR codes vs Photos** - Staff currently focus on QR codes, photos are prepared but dormant

---

## Recommendation

✅ **YES, you can add staff photos** - The infrastructure is there:
- Add a photo upload field to the staff form (5-10 minutes)
- Wire up the route handler (10-15 minutes)
- Add a display endpoint (5-10 minutes)
- Update templates to show photos (5-10 minutes)

**Total effort: ~30 minutes to full implementation**

Would you like me to implement staff photo upload functionality?
