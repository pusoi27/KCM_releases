"""Fix dashboard seat grid flicker by reusing existing card DOM nodes."""
import re

filepath = r'dist_release/templates/dashboard/_scripts.html'
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

end_marker = '\nfunction buildCheckedList('

start_idx = content.find('function buildSeatGrid(activeSessions){')
if start_idx == -1:
    print("ERROR: buildSeatGrid not found"); exit(1)
# Go back to include the comment line before the function
line_start = content.rfind('\n', 0, start_idx) + 1
comment_start = content.rfind('\n', 0, line_start - 1) + 1
if content[comment_start:line_start].strip().startswith('//'):
    start_idx = comment_start

# Find where the function ends: last \n} before buildCheckedList
end_of_next = content.find(end_marker)
if end_of_next == -1:
    print("ERROR: end marker not found"); exit(1)
# Find the closing } of buildSeatGrid
close_pos = content.rfind('\n}', 0, end_of_next)
end_idx = close_pos + 2  # right after \n}

print(f"Replacing chars {start_idx}-{end_idx}")

new_function = r"""// ---------- Column 2 ----------
function buildSeatGrid(activeSessions){
  const grid=document.getElementById("seatGrid");
  document.getElementById("activeCount").innerText=
    `(${activeSessions.length} active)`;
  const seatLimit = isFullscreenMode ? SEAT_LIMIT_FULLSCREEN : SEAT_LIMIT;
  const seatHeight = isFullscreenMode ? "180px" : "170px";
  const fontSize = isFullscreenMode ? '42px' : '32px';

  // Identify which student IDs are still active
  const incomingIds = new Set();
  activeSessions.forEach(s => {
    const sid = s.id ?? s.student_id ?? s.sid;
    if(sid != null) incomingIds.add(sid);
  });

  // Clear timers only for sessions that ended
  for(const [sid, handle] of activeSeatCards.entries()){
    if(!incomingIds.has(sid)){
      clearInterval(handle.timerId);
      activeSeatCards.delete(sid);
    }
  }

  // Rebuild grid, reusing existing card DOM nodes for ongoing sessions
  grid.innerHTML="";
  const newMap = new Map();

  for(let i=0;i<seatLimit;i++){
    const seatLabel="Seat "+(i+1);
    const col=document.createElement("div"); col.className="";
    const s=activeSessions[i];
    if(s){
      const sid = s.id ?? s.student_id ?? s.sid;
      const existing = activeSeatCards.get(sid);

      if(existing){
        // Reuse existing card — timer keeps running, no flicker
        col.appendChild(existing.card);
        grid.appendChild(col);
        newMap.set(sid, existing);
      } else {
        // New session: build card and start timer
        const start=new Date((s.start_time||"").replace(/\u202f|\u2009/g," "));
        const limit = sessionLimitSeconds(s);

        const card=document.createElement("div");
        card.className="card shadow-sm seat-card";
        card.style.minHeight=seatHeight;
        card.style.height=seatHeight;
        card.style.display="flex";
        card.style.flexDirection="column";

        // Build photo HTML
        const photoSrc = s.photo_data_uri || s.photo_url || '';
        const safeName = escapeHtmlAttr(s.name || 'Student');
        const safePhotoSrc = escapeHtmlAttr(photoSrc);
        const photoHtml = photoSrc
          ? `<div style="width:48px;height:48px;position:relative;flex-shrink:0;">
               <img src="${safePhotoSrc}" alt="${safeName}" style="width:48px;height:48px;object-fit:cover;border-radius:50%;border:2px solid #dee2e6;display:block;">
               <div style="width:48px;height:48px;border-radius:50%;border:2px solid #dee2e6;background:#e9ecef;display:none;align-items:center;justify-content:center;"><i class="bi bi-person" style="font-size:1.4em;color:#adb5bd;"></i></div>
             </div>`
          : `<div style="width:48px;height:48px;border-radius:50%;border:2px solid #dee2e6;background:#e9ecef;display:flex;align-items:center;justify-content:center;flex-shrink:0;"><i class="bi bi-person" style="font-size:1.4em;color:#adb5bd;"></i></div>`;

        // Build subjects badges HTML
        const subjectsList = (s.subjects && s.subjects.length > 0) ? s.subjects : (s.subject ? [s.subject] : []);
        const subjectsHtml = subjectsList.length > 0
          ? subjectsList.map(sub => subjectBadgeHtml(sub)).join('')
          : '';

        card.innerHTML=`
          <div class="d-flex align-items-center gap-2 p-2">
            ${photoHtml}
            <div class="flex-grow-1 overflow-hidden">
              <div class="fw-semibold text-truncate" style="font-size:${fontSize}; line-height:1.2;">${s.name}</div>
              ${subjectsHtml ? `<div class="mt-1">${subjectsHtml}</div>` : ''}
            </div>
          </div>
          <div class="flex-grow-1 d-flex align-items-center justify-content-center">
            <div class="timer fw-bold text-success" style="font-size:${fontSize}; line-height:1.2;">--:--:--</div>
          </div>
          <div class="text-center text-muted" style="font-size:0.7em; padding:0.25rem 0.5rem; border-top:1px solid #e9ecef;">
            Started: ${start.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}
          </div>`;
        card.onclick=async()=>{
          if(confirm("Stop session for "+s.name+"?")){
            try {
              const res = await fetch(`/api/students/stop/${s.id}`,{method:'POST'});
              const j = await res.json().catch(()=>({}));
              if(!res.ok){
                showQuickToast(j.error || 'Failed to stop session', true);
              } else if(j && j.checkout_email_status === 'sent'){
                showCheckoutEmailConfirmation(s.name);
              } else if(j && j.checkout_email_status === 'disabled'){
                showQuickToast(j.checkout_email_message || 'Checkout email is unavailable', true);
              } else if(j && j.checkout_email_status === 'no_email'){
                showQuickToast('No email on file', true);
              } else if(j && (j.checkout_email_status === 'failed' || j.checkout_email_status === 'error')){
                showQuickToast(j.checkout_email_message || 'Checkout email failed', true);
              }
            } catch(err){
              showQuickToast('Failed to stop session', true);
            } finally {
              fetchDashboardData();
            }
          }
        };
        col.appendChild(card); grid.appendChild(col);
        const tEl=card.querySelector(".timer");
        const timerId = setInterval(()=>{
          const diff=Math.floor((new Date()-start)/1000);
          tEl.textContent=formatHHMMSS(diff);
          tEl.classList.toggle("text-danger",diff>limit);
          tEl.classList.toggle("text-success",diff<=limit);
        },1000);
        newMap.set(sid, { card, timerId });
      }
    }else{
      const emptyCard = document.createElement("div");
      emptyCard.className = "card shadow-sm seat-card seat-empty";
      emptyCard.style.minHeight = seatHeight;
      emptyCard.style.height = seatHeight;
      emptyCard.style.display = "flex";
      emptyCard.style.alignItems = "center";
      emptyCard.style.justifyContent = "center";
      emptyCard.style.textAlign = "center";
      emptyCard.innerHTML = `<div class="fw-light text-muted" style="font-size:0.9em;">${seatLabel} Empty</div>`;
      col.appendChild(emptyCard);
      grid.appendChild(col);
    }
  }

  // Swap in the new map (stale entries already cleared above)
  activeSeatCards = newMap;

  // Handle overflow students (seatLimit+)
  const overflowContainer = document.getElementById('overflowStudents');
  const overflowList = document.getElementById('overflowList');
  const overflowStudents = activeSessions.slice(seatLimit);

  if(overflowStudents.length > 0){
    overflowContainer.style.display = 'block';
    overflowList.innerHTML = '';
    overflowStudents.forEach(s => {
      const badge = document.createElement('span');
      badge.className = 'badge bg-warning text-dark';
      badge.textContent = s.name;
      badge.style.cursor = 'pointer';
      badge.onclick = async() => {
        if(confirm(`Stop session for ${s.name}?`)){
          try {
            const res = await fetch(`/api/students/stop/${s.id}`, {method:'POST'});
            const j = await res.json().catch(()=>({}));
            if(!res.ok){
              showQuickToast(j.error || 'Failed to stop session', true);
            } else if(j && j.checkout_email_status === 'sent'){
              showCheckoutEmailConfirmation(s.name);
            } else if(j && j.checkout_email_status === 'disabled'){
              showQuickToast(j.checkout_email_message || 'Checkout email is unavailable', true);
            } else if(j && j.checkout_email_status === 'no_email'){
              showQuickToast('No email on file', true);
            } else if(j && (j.checkout_email_status === 'failed' || j.checkout_email_status === 'error')){
              showQuickToast(j.checkout_email_message || 'Checkout email failed', true);
            }
          } catch(err){
            showQuickToast('Failed to stop session', true);
          } finally {
            fetchDashboardData();
          }
        }
      };
      overflowList.appendChild(badge);
    });
  } else {
    overflowContainer.style.display = 'none';
  }
}"""

new_content = content[:start_idx] + new_function + '\n' + content[end_idx:]
with open(filepath, 'w', encoding='utf-8') as f:
    f.write(new_content)

print("Done. Function replaced successfully.")
