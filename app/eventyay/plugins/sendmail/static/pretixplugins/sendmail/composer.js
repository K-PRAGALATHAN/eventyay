const debounceDelay = 400
const emptyCount = '–'

const form = document.querySelector('[data-composer]')
const fieldset = document.querySelector('[data-audience-filters]')
const countTargets = Array.from(document.querySelectorAll('[data-recipient-count]'))

let debounceTimer = null
let previewedSignature = null
let confirmed = false
let lastEditedField = null
// Responses can arrive out of order, so every lookup carries a generation and a
// late answer for filters the organiser has already moved on from is dropped.
let countGeneration = 0
let previewGeneration = 0

/* ------------------------------------------------------------------ helpers */

function showModal(modal) {
  if (window.jQuery && typeof window.jQuery(modal).modal === 'function') {
    window.jQuery(modal).modal('show')
    return
  }
  modal.classList.add('in')
  modal.style.display = 'block'
}

function hideModal(modal) {
  if (window.jQuery && typeof window.jQuery(modal).modal === 'function') {
    window.jQuery(modal).modal('hide')
    return
  }
  modal.classList.remove('in')
  modal.style.display = 'none'
}

function fieldLabel(field) {
  const own = field.closest('label')
  if (own && own.textContent.trim()) {
    return own.textContent.trim()
  }
  const group = field.closest('.form-group')
  const label = group ? group.querySelector('label') : null
  return label ? label.textContent.trim() : field.name
}

function audienceFields() {
  return Array.from(fieldset.querySelectorAll('input, select, textarea')).filter(
    (field) => field.name && !field.disabled && field.type !== 'file' && field.type !== 'hidden'
  )
}

/* ------------------------------------------------------- recipient count/list */

function collectFilters(preview) {
  const data = new FormData()
  const token = form.querySelector('[name=csrfmiddlewaretoken]')
  if (token) {
    data.append('csrfmiddlewaretoken', token.value)
  }
  if (preview) {
    data.append('preview', '1')
  }

  fieldset.querySelectorAll('input, select, textarea').forEach((field) => {
    if (!field.name || field.disabled || field.type === 'file') {
      return
    }
    if ((field.type === 'checkbox' || field.type === 'radio') && !field.checked) {
      return
    }
    if (field.tagName === 'SELECT' && field.multiple) {
      Array.from(field.selectedOptions).forEach((option) => data.append(field.name, option.value))
      return
    }
    data.append(field.name, field.value)
  })

  return data
}

function filterSignature() {
  return Array.from(collectFilters(false).entries())
    .filter(([key]) => key !== 'csrfmiddlewaretoken')
    .map(([key, value]) => `${key}=${value}`)
    .sort()
    .join('&')
}

async function queryRecipients(preview) {
  const response = await fetch(fieldset.dataset.recipientUrl, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    body: collectFilters(preview),
  })

  if (!response.ok) {
    throw new Error(`Recipient lookup failed with status ${response.status}`)
  }
  return response.json()
}

function setCount(value) {
  countTargets.forEach((target) => {
    target.textContent = value
  })
}

async function refreshCount() {
  if (!countTargets.length) {
    return
  }
  const generation = ++countGeneration
  try {
    const result = await queryRecipients(false)
    if (generation !== countGeneration) {
      return
    }
    setCount(result.valid ? result.count : emptyCount)
  } catch (error) {
    if (generation !== countGeneration) {
      return
    }
    console.error('Could not refresh the recipient count.', error)
    setCount(emptyCount)
  }
}

function scheduleRefresh() {
  window.clearTimeout(debounceTimer)
  debounceTimer = window.setTimeout(refreshCount, debounceDelay)
}

/* -------------------------------------------------------------- filter chips */

function addChip(container, label, onRemove) {
  const chip = document.createElement('span')
  chip.className = 'label label-default'
  chip.style.marginRight = '.4em'
  chip.textContent = label

  const remove = document.createElement('button')
  remove.type = 'button'
  remove.className = 'close'
  remove.style.marginLeft = '.4em'
  remove.style.float = 'none'
  remove.style.fontSize = 'inherit'
  remove.style.color = 'inherit'
  remove.style.opacity = '1'
  remove.setAttribute('aria-label', label)
  remove.textContent = '×'
  remove.addEventListener('click', () => {
    onRemove()
    buildChips()
    scheduleRefresh()
  })

  chip.appendChild(remove)
  container.appendChild(chip)
}

function buildChips() {
  const container = document.querySelector('[data-filter-chips]')
  const row = document.querySelector('[data-filter-chips-row]')
  if (!container) {
    return
  }
  container.replaceChildren()

  const boxesByName = new Map()
  audienceFields().forEach((field) => {
    if (field.type === 'checkbox' && field.name !== 'has_filter_checkins') {
      if (!boxesByName.has(field.name)) {
        boxesByName.set(field.name, [])
      }
      boxesByName.get(field.name).push(field)
    } else if (field.type !== 'checkbox' && field.type !== 'radio' && field.value.trim()) {
      addChip(container, `${fieldLabel(field)}: ${field.value}`, () => {
        field.value = ''
      })
    }
  })

  boxesByName.forEach((boxes) => {
    const checked = boxes.filter((box) => box.checked)
    if (!checked.length) {
      return
    }
    // A whole group selected reads better as one chip than as twenty.
    if (checked.length === boxes.length && boxes.length > 1) {
      const group = boxes[0].closest('.form-group')
      const label = group ? group.querySelector('label') : null
      addChip(container, label ? label.textContent.trim() : boxes[0].name, () => {
        checked.forEach((box) => {
          box.checked = false
        })
      })
      return
    }
    checked.forEach((box) => {
      addChip(container, fieldLabel(box), () => {
        box.checked = false
      })
    })
  })

  if (row) {
    row.hidden = !container.childElementCount
  }
}

/* ------------------------------------------------------ recipient list modal */

function addCell(row, text) {
  const cell = document.createElement('td')
  cell.textContent = text
  row.appendChild(cell)
}

function renderRows(result) {
  const body = document.querySelector('[data-recipient-preview-body]')
  body.replaceChildren()

  if (!result.valid || !result.recipients.length) {
    const row = document.createElement('tr')
    const cell = document.createElement('td')
    cell.colSpan = 8
    cell.className = 'text-muted text-center'
    cell.textContent = body.dataset.emptyLabel
    row.appendChild(cell)
    body.appendChild(row)
    return
  }

  result.recipients.forEach((recipient) => {
    const row = document.createElement('tr')
    addCell(row, recipient.name)
    addCell(row, recipient.email)
    addCell(row, recipient.type)
    addCell(row, recipient.order_codes.join(', '))
    addCell(row, recipient.order_status)
    addCell(row, recipient.products.join(', '))
    addCell(row, recipient.checked_in ? '✓' : '—')
    addCell(row, recipient.reason)
    body.appendChild(row)
  })
}

function renderSummary(result) {
  const summary = document.querySelector('[data-recipient-preview-summary]')
  if (!summary) {
    return
  }
  if (!result.valid) {
    summary.textContent = ''
    return
  }
  summary.textContent = result.truncated
    ? summary.dataset.labelTruncated
        .replace('{shown}', result.recipients.length)
        .replace('{count}', result.count)
    : summary.dataset.labelAll.replace('{count}', result.count)
}

async function openPreview() {
  const modal = document.querySelector('#recipient-preview-modal')
  showModal(modal)
  const generation = ++previewGeneration
  try {
    const result = await queryRecipients(true)
    if (generation !== previewGeneration) {
      return
    }
    renderRows(result)
    renderSummary(result)
    if (result.valid) {
      // This is fresher than any count still in flight.
      countGeneration++
      setCount(result.count)
      previewedSignature = filterSignature()
    }
  } catch (error) {
    if (generation !== previewGeneration) {
      return
    }
    console.error('Could not load the recipient list.', error)
    renderRows({ valid: false, recipients: [] })
  }
}

/* --------------------------------------------------------- placeholder drawer */

function placeholderTarget() {
  if (lastEditedField && document.body.contains(lastEditedField)) {
    return lastEditedField
  }
  // Nothing focused yet: fall back to the message body, then the subject, so
  // the button always does something rather than silently doing nothing.
  return (
    form.querySelector('#compose_message_edit [contenteditable=true]') ||
    form.querySelector('textarea[name^=message]') ||
    form.querySelector('input[name^=subject]')
  )
}

function insertPlaceholder(token) {
  const target = placeholderTarget()
  if (!target) {
    return
  }
  target.focus()

  if (target.isContentEditable) {
    if (!document.execCommand('insertText', false, token)) {
      target.appendChild(document.createTextNode(token))
      target.dispatchEvent(new Event('input', { bubbles: true }))
    }
    return
  }

  const start = target.selectionStart ?? target.value.length
  const end = target.selectionEnd ?? start
  target.setRangeText(token, start, end, 'end')
  target.dispatchEvent(new Event('input', { bubbles: true }))
}

function filterPlaceholders(query) {
  const needle = query.trim().toLowerCase()
  document.querySelectorAll('[data-placeholder-item]').forEach((item) => {
    item.hidden = Boolean(needle) && !item.dataset.search.toLowerCase().includes(needle)
  })
  document.querySelectorAll('[data-placeholder-group]').forEach((group) => {
    group.hidden = !group.querySelector('[data-placeholder-item]:not([hidden])')
  })
}

/* ------------------------------------------------------------------- delivery */

function scheduleChosen() {
  const later = form.querySelector('input[name=delivery][value=later]')
  return Boolean(later && later.checked)
}

function syncDelivery() {
  const scheduleFields = document.querySelector('[data-schedule-fields]')
  const sendButton = document.querySelector('[data-send-button]')
  const later = scheduleChosen()
  if (scheduleFields) {
    scheduleFields.hidden = !later
  }
  if (sendButton) {
    sendButton.textContent = later ? sendButton.dataset.labelSchedule : sendButton.dataset.labelSend
  }
}

/* ----------------------------------------------------------------- test email */

async function sendTestEmail() {
  const input = document.querySelector('[data-test-email]')
  const result = document.querySelector('[data-test-result]')
  const data = new FormData(form)
  data.append('test_email', input.value)

  result.className = 'text-muted'
  result.textContent = '…'

  try {
    const response = await fetch(form.dataset.testUrl, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      body: data,
    })
    const payload = await response.json()
    result.className = payload.sent ? 'text-success' : 'text-danger'
    result.textContent = payload.sent ? payload.message : payload.error
  } catch (error) {
    console.error('Could not send the test email.', error)
    result.className = 'text-danger'
    result.textContent = error.message
  }
}

/* --------------------------------------------------------------- confirmation */

function firstFilledValue(selector) {
  const field = Array.from(form.querySelectorAll(selector)).find((input) => input.value.trim())
  return field ? field.value.trim() : ''
}

function populateConfirmation() {
  const later = scheduleChosen()
  const title = document.querySelector('[data-confirm-title]')
  const button = document.querySelector('[data-confirm-send]')
  const attachment = document.querySelector('[data-confirm-attachment]')
  const warning = document.querySelector('[data-confirm-warning]')
  const files = form.querySelector('input[type=file]')

  title.textContent = later ? title.dataset.labelSchedule : title.dataset.labelSend
  button.textContent = later ? button.dataset.labelSchedule : button.dataset.labelSend

  document.querySelector('[data-confirm-count]').textContent = countTargets.length
    ? countTargets[0].textContent
    : emptyCount
  document.querySelector('[data-confirm-subject]').textContent = firstFilledValue('[name^=subject]')
  document.querySelector('[data-confirm-delivery]').textContent = later
    ? firstFilledValue('[name^=scheduled_at]')
    : document.querySelector('[data-send-button]').dataset.labelSend

  attachment.textContent = files && files.files.length
    ? attachment.dataset.labelOne
    : attachment.dataset.labelNone

  warning.hidden = previewedSignature === null || previewedSignature === filterSignature()
}

/* ------------------------------------------------------------------ behaviour */

if (form && fieldset) {
  fieldset.addEventListener('change', () => {
    buildChips()
    scheduleRefresh()
  })
  fieldset.addEventListener('input', () => {
    buildChips()
    scheduleRefresh()
  })

  document.addEventListener('focusin', (event) => {
    const target = event.target
    if (!form.contains(target)) {
      return
    }
    if (target.isContentEditable || target.matches('input[type=text], textarea')) {
      lastEditedField = target
    }
  })

  const previewButton = document.querySelector('[data-recipient-preview-open]')
  if (previewButton) {
    previewButton.addEventListener('click', openPreview)
  }

  const clearButton = document.querySelector('[data-clear-filters]')
  if (clearButton) {
    clearButton.addEventListener('click', () => {
      fieldset.querySelectorAll('input, select, textarea').forEach((field) => {
        if (field.type === 'checkbox' || field.type === 'radio') {
          field.checked = field.defaultChecked
        } else if (field.tagName === 'SELECT') {
          Array.from(field.options).forEach((option) => {
            option.selected = option.defaultSelected
          })
        } else if (field.type !== 'hidden') {
          field.value = field.defaultValue
        }
      })
      buildChips()
      refreshCount()
    })
  }

  const placeholderButton = document.querySelector('[data-placeholder-open]')
  if (placeholderButton) {
    placeholderButton.addEventListener('click', () => showModal(document.querySelector('#placeholder-modal')))
  }

  const placeholderSearch = document.querySelector('[data-placeholder-search]')
  if (placeholderSearch) {
    placeholderSearch.addEventListener('input', () => filterPlaceholders(placeholderSearch.value))
  }

  document.querySelectorAll('[data-placeholder-insert]').forEach((button) => {
    button.addEventListener('click', () => {
      insertPlaceholder(button.closest('[data-placeholder-item]').dataset.token)
      hideModal(document.querySelector('#placeholder-modal'))
    })
  })

  form.querySelectorAll('input[name=delivery]').forEach((radio) => {
    radio.addEventListener('change', syncDelivery)
  })

  const testButton = document.querySelector('[data-test-send]')
  if (testButton) {
    testButton.addEventListener('click', sendTestEmail)
  }

  form.addEventListener('submit', (event) => {
    if (confirmed || !event.submitter || event.submitter.value !== 'send') {
      return
    }
    event.preventDefault()
    populateConfirmation()
    showModal(document.querySelector('#send-confirm-modal'))
  })

  const confirmButton = document.querySelector('[data-confirm-send]')
  if (confirmButton) {
    confirmButton.addEventListener('click', () => {
      confirmed = true
      const action = document.createElement('input')
      action.type = 'hidden'
      action.name = 'action'
      action.value = 'send'
      form.appendChild(action)
      hideModal(document.querySelector('#send-confirm-modal'))
      form.submit()
    })
  }

  syncDelivery()
  buildChips()
  refreshCount()
}
