from odoo import api,fields,models,Command
from odoo.exceptions import AccessError,UserError,ValidationError
from ..common import guard,decimal

MANAGER='finance_workflows.group_manager'

class Term(models.Model):
    _inherit='account.payment.term'
    lab_credit_controlled=fields.Boolean(string='Lab: Approved Credit Required',default=False)
    def write(self,vals):
        if 'lab_credit_controlled' in vals and not self.env.user.has_group(MANAGER):
            raise AccessError('Only finance managers configure controlled credit terms.')
        return super().write(vals)

class Credit(models.Model):
    _name='finance.credit.application'
    _description='Customer Credit Application'
    _inherit=['mail.thread','mail.activity.mixin']
    _check_company_auto=True
    name=fields.Char(required=True,default='Credit Application')
    company_id=fields.Many2one('res.company',required=True,default=lambda s:s.env.company)
    currency_id=fields.Many2one(related='company_id.currency_id')
    partner_id=fields.Many2one('res.partner',required=True,check_company=True)
    requested_limit=fields.Monetary(required=True)
    payment_term_id=fields.Many2one('account.payment.term',required=True,check_company=True,domain=[('lab_credit_controlled','=',True)])
    reviewer_id=fields.Many2one('res.users')
    requester_id=fields.Many2one('res.users',readonly=True)
    approved_by=fields.Many2one('res.users',readonly=True)
    approved_at=fields.Datetime(readonly=True)
    state=fields.Selection([(s,s.title()) for s in ('draft','submitted','approved','rejected','revoked')],default='draft',readonly=True,tracking=True)
    reject_overdue=fields.Boolean(default=True)
    note=fields.Text()

    @api.constrains('requested_limit','payment_term_id','reviewer_id')
    def _valid(self):
        for r in self:
            if r.requested_limit<=0 or not r.payment_term_id.lab_credit_controlled:
                raise ValidationError('A positive limit and a controlled payment term are required.')
            if r.reviewer_id and r.company_id not in r.reviewer_id.company_ids:
                raise ValidationError('Reviewer must have access to this company.')

    @api.model_create_multi
    def create(self,vals_list):
        for vals in vals_list:
            if vals.get('state','draft')!='draft' or any(vals.get(k) for k in ('requester_id','approved_by','approved_at')):
                raise AccessError('Create a draft application and use workflow actions.')
        return super().create(vals_list)

    def write(self,vals):
        if {'state','requester_id','approved_by','approved_at'}.intersection(vals):
            raise AccessError('Use credit review actions.')
        if {'partner_id','company_id','requested_limit','payment_term_id','reject_overdue','reviewer_id'}.intersection(vals) and any(r.state!='draft' for r in self):
            raise UserError('Submitted or decided applications are immutable. Create a new application.')
        return super().write(vals)

    def action_submit(self):
        guard(self,'finance_workflows.group_user')
        for r in self:
            if r.state!='draft':
                raise UserError('Only a draft may be submitted.')
            super(Credit,r).write({'state':'submitted','requester_id':self.env.uid})
            r.message_post(body='Credit application submitted for independent review.')
        return True

    def action_approve(self):
        guard(self,MANAGER)
        for r in self:
            if r.state!='submitted' or r.requester_id==self.env.user or (r.reviewer_id and r.reviewer_id!=self.env.user):
                raise UserError('A submitted application requires its assigned independent reviewer.')
            partner=r.partner_id.commercial_partner_id
            self.env.cr.execute('SELECT pg_advisory_xact_lock(%s,%s)',(180303,partner.id))
            other=self.search([('partner_id','child_of',partner.id),('company_id','=',r.company_id.id),('state','=','approved')])
            if other:
                raise UserError('Revoke the previous approval before approving a replacement limit.')
            super(Credit,r).write({'state':'approved','approved_by':self.env.uid,'approved_at':fields.Datetime.now()})
            partner.with_company(r.company_id).property_payment_term_id=r.payment_term_id
            r.message_post(body='Credit approved. Limit and overdue policy apply at sales confirmation.')
        return True

    def action_reject(self):
        guard(self,MANAGER)
        for r in self:
            if r.state!='submitted' or not r.note:
                raise UserError('A submitted application and decision note are required.')
            super(Credit,r).write({'state':'rejected'})
            r.message_post(body='Credit application rejected; see decision note.')
        return True

    def action_revoke(self):
        guard(self,MANAGER)
        for r in self:
            if r.state!='approved' or not r.note:
                raise UserError('An approved application and revocation note are required.')
            super(Credit,r).write({'state':'revoked'})
            r.message_post(body='Credit authorization revoked. Existing accounting documents remain unchanged.')
        return True

class Sale(models.Model):
    _inherit='sale.order'
    def action_confirm(self):
        for order in self:
            if not order.payment_term_id.lab_credit_controlled:
                continue
            partner=order.partner_id.commercial_partner_id
            self.env.cr.execute('SELECT pg_advisory_xact_lock(%s,%s)',(180303,partner.id))
            approval=self.env['finance.credit.application'].search([
                ('partner_id','child_of',partner.id),('company_id','=',order.company_id.id),
                ('state','=','approved'),('payment_term_id','=',order.payment_term_id.id)],limit=1)
            if not approval:
                raise UserError('Approved customer credit is required for these payment terms.')
            invoices=self.env['account.move'].search([('company_id','=',order.company_id.id),
                ('commercial_partner_id','=',partner.id),('state','=','posted'),('move_type','in',['out_invoice','out_refund'])])
            overdue=invoices.filtered(lambda m:m.move_type=='out_invoice' and m.amount_residual>0 and m.invoice_date_due and m.invoice_date_due<fields.Date.context_today(order))
            if approval.reject_overdue and overdue:
                raise UserError('Customer has overdue invoices; settle or review the credit policy.')
            exposure=sum(decimal(m.amount_residual_signed) for m in invoices)
            committed=self.search([('company_id','=',order.company_id.id),('partner_id','child_of',partner.id),('state','=','sale')])
            for sale in committed:
                posted=sale.invoice_ids.filtered(lambda m:m.state=='posted' and m.move_type=='out_invoice')
                remaining=max(0,sale.amount_total-sum(posted.mapped('amount_total')))
                exposure+=decimal(sale.currency_id._convert(remaining,sale.company_id.currency_id,sale.company_id,fields.Date.context_today(sale)))
            exposure+=decimal(order.currency_id._convert(order.amount_total,order.company_id.currency_id,order.company_id,fields.Date.context_today(order)))
            if exposure>decimal(approval.requested_limit):
                raise UserError('This order exceeds the approved credit limit, including outstanding invoices and unbilled commitments.')
        return super().action_confirm()

class CommissionPlan(models.Model):
    _name='finance.commission.plan'
    _description='Configurable Commission Plan'
    company_id=fields.Many2one('res.company',required=True,default=lambda s:s.env.company)
    name=fields.Char(required=True)
    active=fields.Boolean(default=True)
    salesperson_id=fields.Many2one('res.users',required=True)
    rate=fields.Float(required=True,default=5)
    basis=fields.Selection([('invoice','Posted Net Sales'),('paid','Proportionally Paid Net Sales'),('margin','Net Sales Less Current Standard Cost')],default='invoice',required=True)
    start_date=fields.Date(required=True,default=fields.Date.context_today)
    end_date=fields.Date()
    @api.constrains('rate','start_date','end_date')
    def _valid(self):
        if any(not 0<=r.rate<=100 or (r.end_date and r.end_date<r.start_date) for r in self):
            raise ValidationError('Use a percentage from 0 to 100 and a valid date range.')

    def action_accrue(self):
        guard(self,MANAGER)
        for plan in self:
            domain=[('company_id','=',plan.company_id.id),('state','=','posted'),
                    ('move_type','in',['out_invoice','out_refund']),('invoice_user_id','=',plan.salesperson_id.id),('invoice_date','>=',plan.start_date)]
            if plan.end_date:
                domain.append(('invoice_date','<=',plan.end_date))
            for move in self.env['account.move'].search(domain):
                amount=decimal(move.currency_id._convert(move.amount_untaxed,plan.company_id.currency_id,plan.company_id,move.invoice_date))
                if plan.basis=='margin':
                    cost=sum(decimal(line.product_id.with_company(plan.company_id).standard_price)*decimal(line.product_uom_id._compute_quantity(line.quantity,line.product_id.uom_id))
                             for line in move.invoice_line_ids.filtered(lambda l:l.product_id and l.display_type=='product'))
                    amount-=cost
                if plan.basis=='paid':
                    amount*=decimal(1-move.amount_residual/abs(move.amount_total)) if move.amount_total else decimal(0)
                signed=float(amount)*(1 if move.move_type=='out_invoice' else -1)
                value=signed*plan.rate/100
                entries=self.env['finance.commission.entry'].search([('plan_id','=',plan.id),('move_id','=',move.id)])
                locked=sum(entries.filtered(lambda e:e.state=='approved').mapped('amount'))
                pending=entries.filtered(lambda e:e.state=='draft')
                difference=plan.company_id.currency_id.round(value-locked)
                if pending:
                    super(CommissionEntry,pending).write({'amount':difference})
                elif difference:
                    self.env['finance.commission.entry'].create({'plan_id':plan.id,'move_id':move.id,'amount':difference})
        return True

    @api.model
    def _cron_accrue(self):
        self.search([('active','=',True)]).action_accrue()

class CommissionEntry(models.Model):
    _name='finance.commission.entry'
    _description='Commission Accrual and Adjustment'
    plan_id=fields.Many2one('finance.commission.plan',required=True,ondelete='restrict')
    company_id=fields.Many2one(related='plan_id.company_id',store=True)
    currency_id=fields.Many2one(related='company_id.currency_id')
    salesperson_id=fields.Many2one(related='plan_id.salesperson_id',store=True)
    move_id=fields.Many2one('account.move',required=True)
    amount=fields.Monetary(required=True)
    state=fields.Selection([('draft','Accrued'),('approved','Approved')],default='draft',readonly=True)
    approved_by=fields.Many2one('res.users',readonly=True)
    def write(self,vals):
        if {'amount','state','approved_by','plan_id','move_id'}.intersection(vals):
            raise AccessError('Accrual and approval actions manage the commission ledger.')
        return super().write(vals)
    @api.model_create_multi
    def create(self,vals_list):
        if not self.env.user.has_group(MANAGER) or any(v.get('state','draft')!='draft' or v.get('approved_by') for v in vals_list):
            raise AccessError('Only finance managers may accrue draft commissions.')
        return super().create(vals_list)
    def action_approve(self):
        guard(self,MANAGER)
        for entry in self:
            if entry.state!='draft':
                raise UserError('This commission entry was already approved.')
            super(CommissionEntry,entry).write({'state':'approved','approved_by':self.env.uid})
        return True

class Statement(models.TransientModel):
    _name='finance.statement'
    _description='Customer Statement Filter'
    company_id=fields.Many2one('res.company',required=True,default=lambda s:s.env.company)
    partner_id=fields.Many2one('res.partner',required=True)
    date_from=fields.Date()
    date_to=fields.Date(default=fields.Date.context_today)
    move_ids=fields.Many2many('account.move',compute='_compute_moves')
    currency_id=fields.Many2one(related='company_id.currency_id')
    outstanding=fields.Monetary(compute='_compute_moves')
    @api.depends('partner_id','company_id','date_from','date_to')
    def _compute_moves(self):
        for report in self:
            domain=[('company_id','=',report.company_id.id),('commercial_partner_id','=',report.partner_id.commercial_partner_id.id),
                    ('state','=','posted'),('move_type','in',['out_invoice','out_refund'])]
            if report.date_from: domain.append(('invoice_date','>=',report.date_from))
            if report.date_to: domain.append(('invoice_date','<=',report.date_to))
            report.move_ids=self.env['account.move'].search(domain,order='invoice_date,id')
            report.outstanding=sum(report.move_ids.mapped('amount_residual_signed'))
    def action_print(self):
        guard(self,'finance_workflows.group_user')
        return self.env.ref('finance_workflows.report_summary').report_action(self)
