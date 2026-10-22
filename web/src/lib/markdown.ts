import MarkdownIt from 'markdown-it'
const markdown = new MarkdownIt({ html: false, linkify: true, breaks: true })
// Chat content must never trigger remote image requests or execute model-supplied HTML.
markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(tokens[index].content || '[图片]')
export const renderMarkdown = (text: string) => markdown.render(text)
