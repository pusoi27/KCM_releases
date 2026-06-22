# Student Card Layout Redesign (v01.01.72)

## Mockup Implemented ✓

You requested:
```
┌─────────────────────┐
│   Sebastian         │  ← Name (centered, no truncation)
├─────────────────────┤
│ [Photo]  │  hh:mm  │  ← Two columns
│ Math     │         │     Left: photo + subjects
│ Reading  │         │     Right: timer
└─────────────────────┘
```

## Changes Made

### HTML Structure (buildSeatGrid function)
**File:** `templates/dashboard/_scripts.html` & `dist_release/templates/dashboard/_scripts.html`

**Old Structure:**
```html
<div class="card shadow-sm seat-card">
  <div class="seat-card-header">
    <div class="seat-name">Name</div>
  </div>
  <div class="d-flex align-items-center justify-content-center gap-2">
    <img /> <!-- Photo -->
    <div class="seat-subjects">Math Badge, Reading Badge</div>
  </div>
  <div class="flex-grow-1 d-flex align-items-center justify-content-center">
    <div class="timer">--:--</div>
  </div>
</div>
```

**New Structure:**
```html
<div class="card shadow-sm seat-card">
  <div class="seat-card-header">
    <div class="seat-name">Sebastian</div>
  </div>
  <div class="seat-card-body">
    <!-- LEFT COLUMN -->
    <div class="column-left">
      <img /> <!-- Photo: 48px circular -->
      <div class="seat-subjects">
        <span class="badge">Math</span>
        <span class="badge">Reading</span>
      </div>
    </div>
    <!-- RIGHT COLUMN -->
    <div class="column-right">
      <div class="timer">01:24</div>
    </div>
  </div>
</div>
```

### CSS Updates
**Files Updated:**
- `static/css/style.css`
- `dist_release/static/css/style.css`

**Key CSS Changes:**

1. **Seat Card Base:**
   ```css
   #seatGrid .seat-card {
     display: flex;
     flex-direction: column;
     height: 100%;
   }
   ```

2. **Header (Student Name):**
   ```css
   #seatGrid .seat-card .seat-card-header {
     flex-shrink: 0;
     padding: 8px;
     display: flex;
     align-items: center;
     justify-content: center;
   }
   
   #seatGrid .seat-card .seat-name {
     font-size: 1.3rem;
     font-weight: 800;
     text-align: center;
     word-break: break-word;        /* ← Prevents truncation */
     overflow-wrap: break-word;     /* ← Allows wrapping */
     hyphens: auto;                 /* ← Enables hyphenation */
   }
   ```

3. **Two-Column Body Layout:**
   ```css
   #seatGrid .seat-card .seat-card-body {
     flex: 1 1 0;
     display: flex;
     gap: 12px;
     padding: 10px;
     align-items: flex-start;
     justify-content: space-between;  /* ← Pushes columns apart */
   }
   ```

4. **Left Column (Photo + Subjects):**
   ```css
   #seatGrid .seat-card .column-left {
     display: flex;
     flex-direction: column;
     align-items: center;
     gap: 6px;
     flex-shrink: 0;
   }
   
   #seatGrid .seat-card .seat-subjects {
     display: flex;
     flex-direction: column;   /* ← Stacks vertically */
     gap: 4px;
     align-items: center;
   }
   ```

5. **Right Column (Timer):**
   ```css
   #seatGrid .seat-card .column-right {
     display: flex;
     align-items: center;
     justify-content: center;
     flex: 1;
   }
   
   #seatGrid .seat-card .timer {
     font-size: clamp(1.2rem, 4vw, 2.2rem);
     font-weight: 700;
     font-family: 'Courier New', monospace;
   }
   ```

## Design Features

✓ **Unified Font Size** - All student names display at same size (1.3rem)  
✓ **No Truncation** - Names break to multiple lines if needed  
✓ **Two-Column Layout** - Photo+subjects left, timer right  
✓ **Stacked Badges** - Subject badges display vertically under photo  
✓ **Fluid Typography** - Timer scales responsively with `clamp()`  
✓ **Responsive Photo** - 48px circular with 2px border  
✓ **Color Coding** - Green timer for on-time, red for overtime  

## Testing

Visit the dashboard at `http://127.0.0.1:5000/`

1. Open "Student Roaster" list
2. Click a student to start a session
3. Student will appear in seat grid with new layout:
   - Name centered at top (no truncation)
   - Photo on left
   - Subject badges stacked under photo
   - Timer on right showing elapsed time

## Version Info
- **Version:** 01.01.72
- **Status:** Deployed and tested
- **User Request:** "Display student name on top centered (same font size for all); under name have two columns - left: picture then underneath active subjects; right: timer hh:mm"
