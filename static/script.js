document.addEventListener('DOMContentLoaded', () => {
  // Constants and State
  const API_BASE = '';
  let draggedCard = null;
  let lastLogTimestamp = 0;

  // DOM Elements
  const columns = {
    'Discovered': document.querySelector('.col-discovered .cards-container'),
    'Tailored': document.querySelector('.col-tailored .cards-container'),
    'Applied': document.querySelector('.col-applied .cards-container'),
    'Emailed': document.querySelector('.col-emailed .cards-container'),
    'Requires Intervention': document.querySelector('.col-intervention .cards-container')
  };

  const columnCounts = {
    'Discovered': document.querySelector('.col-discovered .column-count'),
    'Tailored': document.querySelector('.col-tailored .column-count'),
    'Applied': document.querySelector('.col-applied .column-count'),
    'Emailed': document.querySelector('.col-emailed .column-count'),
    'Requires Intervention': document.querySelector('.col-intervention .column-count')
  };

  const omnibarForm = document.getElementById('omnibar-form');
  const omnibarInput = document.getElementById('omnibar-input');
  const terminalBody = document.getElementById('terminal-body');
  const modal = document.getElementById('intervention-modal');
  const modalClose = document.getElementById('modal-close');
  const btnModalCancel = document.getElementById('btn-modal-cancel');
  const btnModalAction = document.getElementById('btn-modal-action');
  
  // Contacts Modal Elements
  const contactsModal = document.getElementById('contacts-modal');
  const contactsModalClose = document.getElementById('contacts-modal-close');
  const btnContactsModalClose = document.getElementById('btn-contacts-modal-close');
  const contactsTableBody = document.getElementById('contacts-table-body');
  const btnAddContactSubmit = document.getElementById('btn-add-contact-submit');
  const btnSendOutreachSubmit = document.getElementById('btn-send-outreach-submit');
  
  let currentContactsJobId = null;
  let currentContactsList = [];
  
  // Profile Elements
  const profileSkills = document.getElementById('profile-skills');
  const profileProjects = document.getElementById('profile-projects');

  // Initial Load
  init();

  function init() {
    loadJobs();
    loadProfile();
    loadResumeStatus();
    setupDragAndDrop();
    setupEventListeners();
    
    // Poll for logs every 2 seconds
    pollLogs();
    setInterval(pollLogs, 2000);
    
    // Poll for jobs update every 10 seconds to keep UI synced with agent
    setInterval(loadJobs, 10000);
  }

  // Load jobs from API and render
  async function loadJobs() {
    try {
      const response = await fetch(`${API_BASE}/api/jobs`);
      if (!response.ok) throw new Error('Failed to fetch jobs');
      const jobs = await response.json();
      
      // Clear all columns
      Object.values(columns).forEach(container => {
        container.innerHTML = '';
      });

      // Reset counts
      const counts = {
        'Discovered': 0,
        'Tailored': 0,
        'Applied': 0,
        'Emailed': 0,
        'Requires Intervention': 0
      };

      // Populate cards
      jobs.forEach(job => {
        const card = createJobCard(job);
        const colContainer = columns[job.status];
        if (colContainer) {
          colContainer.appendChild(card);
          counts[job.status]++;
        }
      });

      // Update count badges
      Object.keys(columnCounts).forEach(status => {
        columnCounts[status].textContent = counts[status];
      });

    } catch (error) {
      console.error('Error loading jobs:', error);
      showLog('System', `Error loading jobs: ${error.message}`, 'error');
    }
  }

  // Load profile quick view
  async function loadProfile() {
    try {
      const response = await fetch(`${API_BASE}/api/profile`);
      if (!response.ok) throw new Error('Failed to fetch profile');
      const profile = await response.json();
      
      // Render skills
      profileSkills.innerHTML = '';
      if (profile.skills && profile.skills.length > 0) {
        profile.skills.forEach(skill => {
          const tag = document.createElement('span');
          tag.className = 'skill-tag';
          tag.textContent = skill;
          profileSkills.appendChild(tag);
        });
      } else {
        profileSkills.innerHTML = '<span class="text-muted">No skills found</span>';
      }

      // Render projects
      profileProjects.innerHTML = '';
      if (profile.projects && profile.projects.length > 0) {
        profile.projects.forEach(project => {
          const item = document.createElement('div');
          item.className = 'project-item';
          
          const title = document.createElement('div');
          item.appendChild(title);
          
          const label = document.createElement('strong');
          label.className = 'project-title';
          label.textContent = project.title;
          title.appendChild(label);

          if (project.technologies) {
            const techs = document.createElement('span');
            techs.style.fontSize = '0.75rem';
            techs.style.color = '#8b5cf6';
            techs.style.marginLeft = '8px';
            techs.textContent = `(${project.technologies.join(', ')})`;
            title.appendChild(techs);
          }

          const desc = document.createElement('div');
          desc.className = 'project-desc';
          desc.textContent = project.details || '';
          item.appendChild(desc);

          profileProjects.appendChild(item);
        });
      } else {
        profileProjects.innerHTML = '<div class="text-muted">No projects found</div>';
      }

    } catch (error) {
      console.error('Error loading profile:', error);
    }
  }

  // Create HTML structure for Job Card
  function createJobCard(job) {
    const card = document.createElement('div');
    card.className = 'job-card';
    card.setAttribute('draggable', 'true');
    card.dataset.id = job.id;
    card.dataset.status = job.status;

    // Header (Title & Match Score)
    const header = document.createElement('div');
    header.className = 'job-header';
    
    const title = document.createElement('h3');
    title.className = 'job-title';
    title.textContent = job.title;
    header.appendChild(title);

    if (job.match_score !== null) {
      const score = document.createElement('span');
      score.className = `score-badge ${getScoreClass(job.match_score)}`;
      score.textContent = `${job.match_score}%`;
      score.title = job.match_reason || 'LLM Match Evaluation';
      header.appendChild(score);
    }
    card.appendChild(header);

    // Company & Location
    const company = document.createElement('div');
    company.className = 'company-name';
    company.textContent = job.company;
    card.appendChild(company);

    // Meta details
    const meta = document.createElement('div');
    meta.className = 'job-meta';
    
    const loc = document.createElement('span');
    loc.textContent = job.location || 'Remote';
    meta.appendChild(loc);
    card.appendChild(meta);

    // CAPTCHA Thumbnail for Intervention Column
    if (job.status === 'Requires Intervention' && job.screenshot_path) {
      const thumb = document.createElement('div');
      thumb.className = 'captcha-thumbnail';
      
      const img = document.createElement('img');
      img.src = job.screenshot_path;
      img.alt = 'CAPTCHA Screenshot';
      thumb.appendChild(img);

      const overlay = document.createElement('div');
      overlay.className = 'captcha-overlay';
      overlay.textContent = 'Resolve';
      thumb.appendChild(overlay);

      thumb.addEventListener('click', (e) => {
        e.stopPropagation();
        openInterventionModal(job);
      });
      card.appendChild(thumb);
    }

    // Contacts Summary Badge
    if (job.status === 'Tailored' || job.status === 'Applied' || job.status === 'Emailed' || job.status === 'Requires Intervention') {
      const contactsBadge = document.createElement('div');
      contactsBadge.className = 'contacts-badge';
      
      let contactList = [];
      try {
        contactList = job.contacts ? JSON.parse(job.contacts) : [];
      } catch(e) {}
      
      const total = contactList.length;
      const sent = contactList.filter(c => c.status === 'sent').length;
      contactsBadge.innerHTML = `<i class="fas fa-users" style="color: var(--linkedin-blue);"></i> Contacts: ${total} (${sent} sent)`;
      card.appendChild(contactsBadge);
    }

    // Action buttons depending on state
    const actions = document.createElement('div');
    actions.className = 'job-card-actions';

    if (job.resume_path && (job.status === 'Tailored' || job.status === 'Applied' || job.status === 'Emailed')) {
      const btnResume = document.createElement('a');
      btnResume.className = 'btn-card';
      btnResume.href = job.resume_path;
      btnResume.target = '_blank';
      btnResume.innerHTML = '<i class="fas fa-file-pdf"></i> Resume';
      actions.appendChild(btnResume);
    }

    const btnLink = document.createElement('a');
    btnLink.className = 'btn-card';
    btnLink.href = job.url;
    btnLink.target = '_blank';
    btnLink.innerHTML = '<i class="fas fa-external-link-alt"></i> Link';
    actions.appendChild(btnLink);

    if (job.status === 'Tailored' || job.status === 'Applied' || job.status === 'Emailed' || job.status === 'Requires Intervention') {
      const btnOutreach = document.createElement('button');
      btnOutreach.className = 'btn-card btn-card-primary';
      btnOutreach.innerHTML = '<i class="fas fa-paper-plane"></i> Outreach';
      btnOutreach.addEventListener('click', (e) => {
        e.stopPropagation();
        openContactsModal(job);
      });
      actions.appendChild(btnOutreach);
    }

    if (job.status === 'Requires Intervention') {
      const btnResolve = document.createElement('button');
      btnResolve.className = 'btn-card btn-card-primary';
      btnResolve.innerHTML = '<i class="fas fa-tools"></i> Fix';
      btnResolve.addEventListener('click', (e) => {
        e.stopPropagation();
        openInterventionModal(job);
      });
      actions.appendChild(btnResolve);
    }

    card.appendChild(actions);

    // Drag events
    card.addEventListener('dragstart', (e) => {
      draggedCard = card;
      card.classList.add('dragging');
      e.dataTransfer.setData('text/plain', job.id);
    });

    card.addEventListener('dragend', () => {
      card.classList.remove('dragging');
      draggedCard = null;
    });

    return card;
  }

  function getScoreClass(score) {
    if (score >= 80) return 'score-high';
    if (score >= 50) return 'score-medium';
    return 'score-low';
  }

  // Setup Drag and Drop events on columns
  function setupDragAndDrop() {
    Object.entries(columns).forEach(([status, container]) => {
      const columnElement = container.closest('.kanban-column');
      
      columnElement.addEventListener('dragover', (e) => {
        e.preventDefault();
        columnElement.style.background = 'rgba(15, 23, 42, 0.5)';
      });

      columnElement.addEventListener('dragenter', (e) => {
        e.preventDefault();
      });

      columnElement.addEventListener('dragleave', () => {
        columnElement.style.background = 'rgba(15, 23, 42, 0.3)';
      });

      columnElement.addEventListener('drop', async (e) => {
        e.preventDefault();
        columnElement.style.background = 'rgba(15, 23, 42, 0.3)';
        
        const jobId = e.dataTransfer.getData('text/plain');
        if (!jobId) return;

        try {
          const response = await fetch(`${API_BASE}/api/jobs/${jobId}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: status })
          });

          if (!response.ok) throw new Error('Failed to update job status');
          
          showLog('System', `Moved job ID ${jobId} to "${status}"`, 'info');
          
          // Reload board to update counts and order
          loadJobs();

        } catch (error) {
          console.error('Drop error:', error);
          showLog('System', `Failed to move job: ${error.message}`, 'error');
        }
      });
    });
  }

  // Setup Event Listeners
  function setupEventListeners() {
    // Omnibar Profile Update
    omnibarForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const promptText = omnibarInput.value.trim();
      if (!promptText) return;

      showLog('Omnibar', `Processing update: "${promptText}"`, 'info');
      omnibarInput.value = '';

      try {
        const response = await fetch(`${API_BASE}/api/profile/update`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt: promptText })
        });

        if (!response.ok) {
          const errData = await response.json();
          throw new Error(errData.detail || 'Failed to update profile');
        }
        
        const result = await response.json();
        showLog('Omnibar', `Profile updated successfully! ${result.message}`, 'success');
        
        // Reload Profile View
        loadProfile();

      } catch (error) {
        console.error('Omnibar error:', error);
        showLog('Omnibar', `Update failed: ${error.message}`, 'error');
      }
    });

    // Close Modal
    modalClose.addEventListener('click', () => { modal.style.display = 'none'; });
    btnModalCancel.addEventListener('click', () => { modal.style.display = 'none'; });
    
    // Contacts Modal Close
    const closeAndSaveContacts = async () => {
      if (currentContactsJobId && currentContactsList) {
        try {
          await fetch(`${API_BASE}/api/jobs/${currentContactsJobId}/contacts`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ contacts: currentContactsList })
          });
        } catch (err) {
          console.error("Auto-saving contacts failed:", err);
        }
      }
      contactsModal.style.display = 'none';
      loadJobs();
    };

    contactsModalClose.addEventListener('click', closeAndSaveContacts);
    btnContactsModalClose.addEventListener('click', closeAndSaveContacts);
    
    // Close modal on click outside
    window.addEventListener('click', (e) => {
      if (e.target === modal) modal.style.display = 'none';
      if (e.target === contactsModal) closeAndSaveContacts();
    });

    // Resume Override elements
    const resumeFileInput = document.getElementById('resume-file-input');
    const btnResumeUpload = document.getElementById('btn-resume-upload');
    const btnResumeDelete = document.getElementById('btn-resume-delete');
    const fileLabel = document.getElementById('file-label-text');

    resumeFileInput.addEventListener('change', () => {
      if (resumeFileInput.files.length > 0) {
        fileLabel.textContent = `Selected: ${resumeFileInput.files[0].name}`;
      } else {
        fileLabel.textContent = 'Select fresh static resume PDF to use globally...';
      }
    });

    btnResumeUpload.addEventListener('click', async () => {
      if (resumeFileInput.files.length === 0) {
        showLog('System', 'No PDF file selected to upload', 'warning');
        return;
      }
      
      const file = resumeFileInput.files[0];
      const formData = new FormData();
      formData.append('file', file);
      
      showLog('System', `Uploading default resume: ${file.name}...`, 'info');
      
      try {
        const response = await fetch(`${API_BASE}/api/resume/upload`, {
          method: 'POST',
          body: formData
        });
        
        if (!response.ok) {
          const err = await response.json();
          throw new Error(err.detail || 'Upload failed');
        }
        
        showLog('System', 'Default resume uploaded successfully! Reverted to static mode.', 'success');
        resumeFileInput.value = '';
        loadResumeStatus();
      } catch (error) {
        console.error('Upload error:', error);
        showLog('System', `Upload failed: ${error.message}`, 'error');
      }
    });

    btnResumeDelete.addEventListener('click', async () => {
      showLog('System', 'Deleting static resume override...', 'info');
      try {
        const response = await fetch(`${API_BASE}/api/resume`, {
          method: 'DELETE'
        });
        
        if (!response.ok) throw new Error('Deletion failed');
        
        showLog('System', 'Static resume override removed. Reverted to Dynamic Tailored Resumes.', 'success');
        loadResumeStatus();
      } catch (error) {
        console.error('Delete error:', error);
        showLog('System', `Deletion failed: ${error.message}`, 'error');
      }
    });
  }

  // Open Captcha Intervention Modal
  function openInterventionModal(job) {
    const titleEl = document.getElementById('modal-job-title');
    const companyEl = document.getElementById('modal-job-company');
    const linkEl = document.getElementById('modal-job-link');
    const imgEl = document.getElementById('modal-captcha-image');

    titleEl.textContent = job.title;
    companyEl.textContent = job.company;
    linkEl.href = job.url;
    imgEl.src = job.screenshot_path || '';
    
    // Set up button actions dynamically
    btnModalAction.onclick = async () => {
      try {
        const response = await fetch(`${API_BASE}/api/jobs/${job.id}/status`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: 'Applied' })
        });

        if (!response.ok) throw new Error('Failed to update job status');
        
        showLog('System', `Manual intervention resolved for ${job.company}. Job marked as "Applied".`, 'success');
        modal.style.display = 'none';
        loadJobs();

      } catch (error) {
        console.error('Modal resolution error:', error);
        showLog('System', `Failed to update status: ${error.message}`, 'error');
      }
    };

    modal.style.display = 'flex';
  }

  // Poll Logs from server
  async function pollLogs() {
    try {
      const response = await fetch(`${API_BASE}/api/logs?since=${lastLogTimestamp}`);
      if (!response.ok) return;
      const logs = await response.json();
      
      if (logs.length > 0) {
        logs.forEach(log => {
          appendLogToTerminal(log);
          if (log.timestamp > lastLogTimestamp) {
            lastLogTimestamp = log.timestamp;
          }
        });
        
        // Scroll terminal to bottom
        terminalBody.scrollTop = terminalBody.scrollHeight;
      }
    } catch (error) {
      console.error('Error polling logs:', error);
    }
  }

  // Append a single log message to the UI terminal
  function appendLogToTerminal(log) {
    const entry = document.createElement('div');
    entry.className = 'log-entry';

    const timeSpan = document.createElement('span');
    timeSpan.className = 'log-time';
    const date = new Date(log.timestamp * 1000);
    timeSpan.textContent = `[${date.toLocaleTimeString()}] `;
    entry.appendChild(timeSpan);

    const moduleSpan = document.createElement('span');
    moduleSpan.style.color = '#a855f7';
    moduleSpan.style.fontWeight = 'bold';
    moduleSpan.textContent = `${log.module}: `;
    entry.appendChild(moduleSpan);

    const messageSpan = document.createElement('span');
    messageSpan.className = `log-${log.level.toLowerCase()}`;
    messageSpan.textContent = log.message;
    entry.appendChild(messageSpan);

    terminalBody.appendChild(entry);

    // Cap the logs displayed in terminal to 200 rows
    while (terminalBody.childElementCount > 200) {
      terminalBody.removeChild(terminalBody.firstChild);
    }
  }

  async function loadResumeStatus() {
    try {
      const response = await fetch(`${API_BASE}/api/resume/status`);
      if (!response.ok) throw new Error('Failed to fetch resume status');
      const status = await response.json();
      
      const badge = document.getElementById('resume-status-badge');
      const btnDelete = document.getElementById('btn-resume-delete');
      const fileLabel = document.getElementById('file-label-text');
      
      if (status.has_default) {
        badge.textContent = 'Active: Static Resume (default_resume.pdf)';
        badge.style.background = 'rgba(5, 118, 66, 0.1)';
        badge.style.borderColor = 'rgba(5, 118, 66, 0.2)';
        badge.style.color = '#057642';
        btnDelete.style.display = 'block';
        fileLabel.textContent = 'default_resume.pdf is active. Click to select a new one...';
      } else {
        badge.textContent = 'Active: Dynamic Tailored Resumes';
        badge.style.background = 'rgba(10, 102, 194, 0.1)';
        badge.style.borderColor = 'rgba(10, 102, 194, 0.2)';
        badge.style.color = '#0a66c2';
        btnDelete.style.display = 'none';
        fileLabel.textContent = 'Select fresh static resume PDF to use globally...';
      }
    } catch (error) {
      console.error('Error loading resume status:', error);
    }
  }

  // Open Outreach Contacts Modal
  async function openContactsModal(job) {
    currentContactsJobId = job.id;
    currentContactsList = [];
    
    // Set title
    document.getElementById('contacts-modal-title').textContent = `Outreach Contacts - ${job.company}`;
    
    // Clear add contact inputs
    document.getElementById('new-contact-name').value = '';
    document.getElementById('new-contact-role').value = '';
    document.getElementById('new-contact-email').value = '';
    
    // Show spinner or placeholder in table
    contactsTableBody.innerHTML = '<tr><td colspan="5" style="text-align: center; padding: 1.5rem;"><i class="fas fa-spinner fa-spin"></i> Loading contacts...</td></tr>';
    
    contactsModal.style.display = 'flex';
    
    try {
      const response = await fetch(`${API_BASE}/api/jobs/${job.id}/contacts`);
      if (!response.ok) throw new Error('Failed to fetch contacts');
      currentContactsList = await response.json();
      renderContactsTable();
    } catch (error) {
      console.error("Fetch contacts error:", error);
      contactsTableBody.innerHTML = '<tr><td colspan="5" style="text-align: center; padding: 1.5rem; color: var(--accent-red);">Failed to load contacts.</td></tr>';
    }
  }

  function renderContactsTable() {
    contactsTableBody.innerHTML = '';
    
    if (currentContactsList.length === 0) {
      contactsTableBody.innerHTML = '<tr><td colspan="5" style="text-align: center; padding: 1.5rem; color: var(--color-text-muted);">No contacts configured. Use the form below to add one.</td></tr>';
      return;
    }
    
    currentContactsList.forEach((contact, index) => {
      const tr = document.createElement('tr');
      
      // Name
      const tdName = document.createElement('td');
      const inputName = document.createElement('input');
      inputName.type = 'text';
      inputName.className = 'contacts-input';
      inputName.value = contact.name || '';
      inputName.addEventListener('change', (e) => {
        currentContactsList[index].name = e.target.value;
      });
      tdName.appendChild(inputName);
      tr.appendChild(tdName);
      
      // Role
      const tdRole = document.createElement('td');
      const inputRole = document.createElement('input');
      inputRole.type = 'text';
      inputRole.className = 'contacts-input';
      inputRole.value = contact.role || '';
      inputRole.addEventListener('change', (e) => {
        currentContactsList[index].role = e.target.value;
      });
      tdRole.appendChild(inputRole);
      tr.appendChild(tdRole);
      
      // Email
      const tdEmail = document.createElement('td');
      const inputEmail = document.createElement('input');
      inputEmail.type = 'email';
      inputEmail.className = 'contacts-input';
      inputEmail.value = contact.email || '';
      inputEmail.addEventListener('change', (e) => {
        currentContactsList[index].email = e.target.value;
      });
      tdEmail.appendChild(inputEmail);
      tr.appendChild(tdEmail);
      
      // Status
      const tdStatus = document.createElement('td');
      const badge = document.createElement('span');
      badge.className = `contact-status status-${contact.status || 'pending'}`;
      badge.textContent = contact.status || 'pending';
      tdStatus.appendChild(badge);
      tr.appendChild(tdStatus);
      
      // Delete Button
      const tdAction = document.createElement('td');
      tdAction.style.textAlign = 'center';
      const btnDelete = document.createElement('button');
      btnDelete.className = 'btn-delete-contact';
      btnDelete.innerHTML = '<i class="fas fa-trash-alt"></i>';
      btnDelete.title = 'Delete Contact';
      btnDelete.addEventListener('click', () => {
        currentContactsList.splice(index, 1);
        renderContactsTable();
      });
      tdAction.appendChild(btnDelete);
      tr.appendChild(tdAction);
      
      contactsTableBody.appendChild(tr);
    });
  }

  // Add contact listener
  btnAddContactSubmit.addEventListener('click', () => {
    const nameInput = document.getElementById('new-contact-name');
    const roleInput = document.getElementById('new-contact-role');
    const emailInput = document.getElementById('new-contact-email');
    
    const name = nameInput.value.trim();
    const role = roleInput.value.trim();
    const email = emailInput.value.trim();
    
    if (!email) {
      showLog('System', 'Email address is required to add contact.', 'warning');
      return;
    }
    
    // Add to list
    const isExecutive = ['founder', 'co-founder', 'ceo', 'cto', 'president', 'vp', 'director'].some(w => role.toLowerCase().includes(w));
    currentContactsList.push({
      name: name || role,
      role: role || 'Recruiter',
      email: email,
      pitch_type: isExecutive ? 'executive' : 'hr',
      status: 'pending'
    });
    
    // Clear inputs
    nameInput.value = '';
    roleInput.value = '';
    emailInput.value = '';
    
    // Re-render
    renderContactsTable();
  });

  // Send outreach listener
  btnSendOutreachSubmit.addEventListener('click', async () => {
    if (!currentContactsJobId) return;
    
    // 1. Save contacts list to server first to persist modifications
    try {
      await fetch(`${API_BASE}/api/jobs/${currentContactsJobId}/contacts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ contacts: currentContactsList })
      });
    } catch(err) {
      console.error("Saving contacts failed before outreach:", err);
    }
    
    // 2. Trigger outreach
    btnSendOutreachSubmit.disabled = true;
    btnSendOutreachSubmit.innerHTML = '<span class="spinner"></span> Sending Emails...';
    showLog('Emailer', `Starting outreach process for Job ID ${currentContactsJobId}...`, 'info');
    
    try {
      const response = await fetch(`${API_BASE}/api/jobs/${currentContactsJobId}/outreach`, {
        method: 'POST'
      });
      const data = await response.json();
      
      if (response.ok && data.status === 'success') {
        showLog('Emailer', `Outreach successful: ${data.message}`, 'success');
      } else {
        showLog('Emailer', `Outreach completed with issues: ${data.message}`, 'warning');
      }
      
      // Re-fetch contacts to update table statuses
      const getRes = await fetch(`${API_BASE}/api/jobs/${currentContactsJobId}/contacts`);
      if (getRes.ok) {
        currentContactsList = await getRes.json();
        renderContactsTable();
      }
      
    } catch(error) {
      console.error("Outreach error:", error);
      showLog('Emailer', `Outreach request failed: ${error.message}`, 'error');
    } finally {
      btnSendOutreachSubmit.disabled = false;
      btnSendOutreachSubmit.innerHTML = '<i class="fas fa-paper-plane"></i> Send Cold Emails Now';
    }
  });

  // Helper to log locally in terminal if server is slow
  function showLog(module, message, level = 'info') {
    const log = {
      timestamp: Date.now() / 1000,
      module: module,
      message: message,
      level: level
    };
    appendLogToTerminal(log);
    terminalBody.scrollTop = terminalBody.scrollHeight;
  }
});
