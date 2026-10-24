import { Modal } from '../common';
import { TERMS_SECTIONS, TERMS_VERSION } from '../../services/terms';

interface TermsDialogProps {
  open: boolean;
  onClose: () => void;
}

/**
 * 用户协议（含免责条款）全文弹层。
 *
 * 为什么用弹层而不是独立路由：登录闸门在**未登录时**会把整棵路由树挡住，
 * `/terms` 在登录前根本到不了；而条款恰恰是**登录前**必须能读到的。
 * 这里做成弹层，可在登录前阅读，且不会丢掉用户已经填了一半的表单。
 */
export default function TermsDialog({ open, onClose }: TermsDialogProps) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="用户协议与免责条款"
      width="lg"
      data-testid="terms-dialog"
    >
      <div
        role="region"
        aria-label="协议正文"
        tabIndex={0}
        className="max-h-[60vh] space-y-4 overflow-y-auto pr-1 text-sm leading-6 text-surface-600 outline-offset-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary-500 dark:text-surface-300"
      >
        <p className="text-xs text-surface-400">版本：{TERMS_VERSION}</p>
        <p>阅读后请返回登录或注册表单，自行勾选同意；关闭本窗口不会自动表示同意。</p>
        {TERMS_SECTIONS.map((section) => (
          <section key={section.heading}>
            <h3 className="mb-1 font-semibold text-surface-800 dark:text-surface-100">{section.heading}</h3>
            {section.paragraphs.map((paragraph) => (
              <p key={paragraph} className="mb-1.5 whitespace-pre-wrap">{paragraph}</p>
            ))}
          </section>
        ))}
      </div>
      <div className="mt-4 flex justify-end">
        <button type="button" className="btn-primary min-h-[44px]" onClick={onClose} data-testid="terms-close">
          返回登录 / 注册
        </button>
      </div>
    </Modal>
  );
}
