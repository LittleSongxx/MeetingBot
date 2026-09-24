const apiBaseUrl = (import.meta.env.VITE_BASE_URL || '/api').replace(/\/$/, '')

export function fileUrl(value) {
    if (!value || typeof value !== 'string') return ''

    // Imported sample data and older localStorage entries use the old backend port.
    const legacyFileUrl = /^https?:\/\/(?:127\.0\.0\.1|localhost):9090(\/files\/.*)$/i.exec(value)
    const filePath = legacyFileUrl ? legacyFileUrl[1] : value
    if (filePath.startsWith('/files/')) return apiBaseUrl + filePath

    return value
}
