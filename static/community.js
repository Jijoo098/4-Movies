(function () {
  'use strict';

  const hub = document.querySelector('.community-hub');
  if (!hub) return;

  /* Mobile drawer navigation */
  const leftSidebar = document.getElementById('communitySidebarLeft');
  const rightSidebar = document.getElementById('communitySidebarRight');
  let backdrop = document.querySelector('.community-drawer-backdrop');

  if (!backdrop) {
    backdrop = document.createElement('div');
    backdrop.className = 'community-drawer-backdrop';
    backdrop.setAttribute('aria-hidden', 'true');
    document.body.appendChild(backdrop);
  }

  function closeDrawers() {
    leftSidebar?.classList.remove('is-drawer-open');
    rightSidebar?.classList.remove('is-drawer-open');
    backdrop.classList.remove('is-visible');
    document.body.style.overflow = '';
  }

  function openDrawer(side) {
    closeDrawers();
    const panel = side === 'right' ? rightSidebar : leftSidebar;
    panel?.classList.add('is-drawer-open');
    backdrop.classList.add('is-visible');
    document.body.style.overflow = 'hidden';
  }

  hub.querySelectorAll('[data-drawer]').forEach((btn) => {
    btn.addEventListener('click', () => openDrawer(btn.dataset.drawer));
  });

  backdrop.addEventListener('click', closeDrawers);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeDrawers();
  });

  /* Scroll to composer */
  hub.querySelectorAll('[data-scroll-composer]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const composer = document.getElementById('communityComposer');
      if (!composer) return;
      closeDrawers();
      composer.scrollIntoView({ behavior: 'smooth', block: 'start' });
      composer.querySelector('textarea')?.focus();
    });
  });

  /* Composer toolbar */
  const composeForm = document.getElementById('communityComposeForm');
  if (composeForm) {
    const mediaInput = document.getElementById('communityMedia');
    const preview = document.getElementById('communityMediaPreview');
    const textarea = composeForm.querySelector('.community-compose__input');

    composeForm.querySelectorAll('[data-compose-tool="media"]').forEach((btn) => {
      btn.addEventListener('click', () => {
        mediaInput?.click();
      });
    });

    if (mediaInput && preview) {
      mediaInput.addEventListener('change', () => {
        const file = mediaInput.files && mediaInput.files[0];
        if (!file) {
          preview.hidden = true;
          preview.innerHTML = '';
          return;
        }
        preview.hidden = false;
        preview.innerHTML = '';
        const url = URL.createObjectURL(file);
        if (file.type.startsWith('video/')) {
          const video = document.createElement('video');
          video.controls = true;
          video.playsInline = true;
          video.src = url;
          preview.appendChild(video);
        } else {
          const img = document.createElement('img');
          img.src = url;
          img.alt = 'Upload preview';
          preview.appendChild(img);
        }
      });
    }

    composeForm.addEventListener('submit', (event) => {
      if (!textarea) return;
      const hasText = textarea.value.trim().length > 0;
      const hasMedia = !!(mediaInput?.files && mediaInput.files.length);
      if (!hasText && !hasMedia) {
        event.preventDefault();
        window.alert('Add text or a photo/video before posting.');
      }
    });
  }

  /* Share / copy link */
  hub.querySelectorAll('.community-share').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const url = btn.dataset.shareUrl;
      if (!url) return;
      const labelEl = btn.querySelector('span');
      const label = labelEl ? labelEl.textContent.trim() : 'Share';
      try {
        if (navigator.share) {
          await navigator.share({ url, title: '4 Movies Community' });
          return;
        }
        await navigator.clipboard.writeText(url);
        if (labelEl) labelEl.textContent = 'Copied';
        window.setTimeout(() => { if (labelEl) labelEl.textContent = label; }, 1500);
      } catch (err) {
        window.prompt('Copy this link:', url);
      }
    });
  });

  /* Spoiler reveal on click */
  hub.querySelectorAll('.community-card__text--spoiler').forEach((el) => {
    el.addEventListener('click', () => el.classList.add('is-revealed'));
  });
})();
