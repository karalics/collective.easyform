# -*- coding: utf-8 -*-

from BTrees.IOBTree import IOBTree
try:
    from BTrees.LOBTree import LOBTree
    SavedDataBTree = LOBTree
except ImportError:
    SavedDataBTree = IOBTree
from DateTime import DateTime
from Products.Archetypes.utils import OrderedDict
from Products.CMFCore.utils import getToolByName
from Products.PageTemplates.ZopePageTemplate import ZopePageTemplate
from Products.PythonScripts.PythonScript import PythonScript
from StringIO import StringIO
from copy import deepcopy
from csv import writer as csvwriter
from email import Encoders
from email.Header import Header
from email.MIMEAudio import MIMEAudio
from email.MIMEBase import MIMEBase
from email.MIMEImage import MIMEImage
from email.MIMEMultipart import MIMEMultipart
from email.MIMEText import MIMEText
from email.utils import formataddr
from logging import getLogger
from plone.namedfile.interfaces import INamedFile
from plone.supermodel.exportimport import BaseHandler
from time import time
from types import StringTypes
from zope import schema as zs
from zope.component import getUtilitiesFor
from zope.contenttype import guess_content_type
from zope.i18n import translate
from zope.interface import implements
from zope.schema import getFieldsInOrder
from zope.schema.vocabulary import SimpleVocabulary

from collective.formulator import formulatorMessageFactory as _
from collective.formulator.api import DollarVarReplacer
from collective.formulator.api import get_context
from collective.formulator.api import get_expression
from collective.formulator.api import get_fields
from collective.formulator.interfaces import IAction
from collective.formulator.interfaces import IActionFactory
from collective.formulator.interfaces import ICustomScript
from collective.formulator.interfaces import IExtraData
from collective.formulator.interfaces import IFieldExtender
from collective.formulator.interfaces import IMailer
from collective.formulator.interfaces import ISaveData

logger = getLogger('collective.formulator')


def FormulatorActionsVocabularyFactory(context):
    field_factories = getUtilitiesFor(IActionFactory)
    terms = []
    for (id, factory) in field_factories:
        terms.append(SimpleVocabulary.createTerm(
            factory, translate(factory.title), factory.title))
    return SimpleVocabulary(terms)


class ActionFactory(object):
    implements(IActionFactory)

    title = u''

    def __init__(self, fieldcls, title, *args, **kw):
        self.fieldcls = fieldcls
        self.title = title
        self.args = args
        self.kw = kw

    def editable(self, field):
        """ test whether a given instance of a field is editable """
        return True

    def __call__(self, *args, **kw):
        kwargs = deepcopy(self.kw)
        kwargs.update(**kw)
        return self.fieldcls(*(self.args + args), **kwargs)


class Action(zs.Bool):

    """ Base action class """
    implements(IAction)

    def onSuccess(self, fields, request):
        raise NotImplementedError(
            "There is not implemented 'onSuccess' of %r" % (self,))


class Mailer(Action):
    implements(IMailer)
    __doc__ = IMailer.__doc__

    def __init__(self, **kw):
        for i, f in IMailer.namesAndDescriptions():
            setattr(self, i, kw.pop(i, f.default))
        super(Mailer, self).__init__(**kw)

    def secure_header_line(self, line):
        nlpos = line.find('\x0a')
        if nlpos >= 0:
            line = line[:nlpos]
        nlpos = line.find('\x0d')
        if nlpos >= 0:
            line = line[:nlpos]
        return line

    def _destFormat(self, input):
        """ Format destination (To) input.
            Input may be a string or sequence of strings;
            returns a well-formatted address field
        """

        if type(input) in StringTypes:
            input = [s for s in input.split(',')]
        input = [s for s in input if s]
        filtered_input = [s.strip().encode('utf-8') for s in input]

        if filtered_input:
            return '<%s>' % '>, <'.join(filtered_input)
        else:
            return ''

    def get_mail_body(self, fields, request, **kwargs):
        """Returns the mail-body with footer.
        """

        context = get_context(self)
        schema = get_fields(context)
        all_fields = [f for f in fields
                      # TODO
                      # if not (f.isLabel() or f.isFileField()) and not (getattr(self,
                      # 'showAll', True) and f.getServerSide())]
                      if not (INamedFile.providedBy(fields[f])) and not (getattr(self, 'showAll', True) and IFieldExtender(schema[f]).serverSide)
                      ]

        # which fields should we show?
        if getattr(self, 'showAll', True):
            live_fields = all_fields
        else:
            live_fields = [
                f for f in all_fields if f in getattr(self, 'showFields', ())]

        if not getattr(self, 'includeEmpties', True):
            all_fields = live_fields
            live_fields = [f for f in all_fields if fields[f]]
            for f in all_fields:
                value = fields[f]
                if value:
                    live_fields.append(f)

        #bare_fields = [schema[f] for f in live_fields]
        bare_fields = dict([(f, fields[f]) for f in live_fields])
        bodyfield = self.body_pt

        # pass both the bare_fields (fgFields only) and full fields.
        # bare_fields for compatability with older templates,
        # full fields to enable access to htmlValue
        replacer = DollarVarReplacer(fields).sub
        extra = {
            'data': bare_fields,
            'fields': dict([(i, j.title) for i, j in getFieldsInOrder(schema)]),
            'mailer': self,
            'body_pre': self.body_pre and replacer(self.body_pre),
            'body_post': self.body_post and replacer(self.body_post),
            'body_footer': self.body_footer and replacer(self.body_footer),
        }
        template = ZopePageTemplate(self.__name__)
        template.write(bodyfield)
        template = template.__of__(context)
        body = template.pt_render(extra_context=extra)

        # if isinstance(body, unicode):
            #body = body.encode("utf-8")

        #keyid = getattr(self, 'gpg_keyid', None)
        #encryption = gpg and keyid

        # if encryption:
            #bodygpg = gpg.encrypt(body, keyid)
            # if bodygpg.strip():
                #body = bodygpg

        return body

    def get_header_body_tuple(self, fields, request,
                              from_addr=None, to_addr=None,
                              subject=None, **kwargs):
        """Return header and body of e-mail as an 3-tuple:
        (header, additional_header, body)

        header is a dictionary, additional header is a list, body is a StringIO

        Keyword arguments:
        request -- (optional) alternate request object to use
        """
        context = get_context(self)
        pprops = getToolByName(context, 'portal_properties')
        site_props = getToolByName(pprops, 'site_properties')
        portal = getToolByName(context, 'portal_url').getPortalObject()
        pms = getToolByName(context, 'portal_membership')
        utils = getToolByName(context, 'plone_utils')

        body = self.get_mail_body(fields, request, **kwargs)

        # fields = self.fgFields()

        # get Reply-To
        reply_addr = None
        if hasattr(self, 'replyto_field'):
            reply_addr = request.form.get(self.replyto_field, None)

        # get subject header
        nosubject = '(no subject)'
        if hasattr(self, 'subjectOverride') and self.subjectOverride and get_expression(context, self.subjectOverride):
            # subject has a TALES override
            subject = get_expression(context, self.subjectOverride).strip()
        else:
            subject = getattr(self, 'msg_subject', nosubject)
            subjectField = request.form.get(self.subject_field, None)
            if subjectField is not None:
                subject = subjectField
            else:
                # we only do subject expansion if there's no field chosen
                subject = DollarVarReplacer(fields).sub(subject)

        # Get From address
        if hasattr(self, 'senderOverride') and self.senderOverride and get_expression(context, self.senderOverride):
            from_addr = get_expression(context, self.senderOverride).strip()
        else:
            from_addr = from_addr or site_props.getProperty('email_from_address') or \
                portal.getProperty('email_from_address')

        # Get To address and full name
        if hasattr(self, 'recipientOverride') and self.recipientOverride and get_expression(context, self.recipientOverride):
            recip_email = get_expression(context, self.recipientOverride)
        else:
            recip_email = None
            if hasattr(self, 'to_field') and self.to_field:
                recip_email = request.form.get(self.to_field, None)
            if not recip_email:
                recip_email = self.recipient_email

        recip_email = self._destFormat(recip_email)

        recip_name = self.recipient_name.encode('utf-8')

        # if no to_addr and no recip_email specified, use owner adress if possible.
        # if not, fall back to portal email_from_address.
        # if still no destination, raise an assertion exception.
        if not recip_email and not to_addr:
            ownerinfo = context.getOwner()
            ownerid = ownerinfo.getId()
            fullname = ownerid
            userdest = pms.getMemberById(ownerid)
            if userdest is not None:
                fullname = userdest.getProperty('fullname', ownerid)
            toemail = ''
            if userdest is not None:
                toemail = userdest.getProperty('email', '')
            if not toemail:
                toemail = portal.getProperty('email_from_address')
            assert toemail, """
                    Unable to mail form input because no recipient address has been specified.
                    Please check the recipient settings of the Formulator "Mailer" within the
                    current form folder.
                """
            to = formataddr((fullname, toemail))
        else:
            to = to_addr or '%s %s' % (recip_name, recip_email)

        headerinfo = OrderedDict()

        headerinfo['To'] = self.secure_header_line(to)
        headerinfo['From'] = self.secure_header_line(from_addr)
        if reply_addr:
            headerinfo['Reply-To'] = self.secure_header_line(reply_addr)

        # transform subject into mail header encoded string
        email_charset = portal.getProperty('email_charset', 'utf-8')

        if not isinstance(subject, unicode):
            site_charset = utils.getSiteEncoding()
            subject = unicode(subject, site_charset, 'replace')

        msgSubject = self.secure_header_line(
            subject).encode(email_charset, 'replace')
        msgSubject = str(Header(msgSubject, email_charset))
        headerinfo['Subject'] = msgSubject

        # CC
        cc_recips = filter(None, self.cc_recipients)
        if hasattr(self, 'ccOverride') and self.ccOverride and get_expression(context, self.ccOverride):
            cc_recips = get_expression(context, self.ccOverride)
        if cc_recips:
            headerinfo['Cc'] = self._destFormat(cc_recips)

        # BCC
        bcc_recips = filter(None, self.bcc_recipients)
        if hasattr(self, 'bccOverride') and self.bccOverride and get_expression(context, self.bccOverride):
            bcc_recips = get_expression(context, self.bccOverride)
        if bcc_recips:
            headerinfo['Bcc'] = self._destFormat(bcc_recips)

        for key in getattr(self, 'xinfo_headers', []):
            headerinfo['X-%s' % key] = self.secure_header_line(
                request.get(key, 'MISSING'))

        # return 3-Tuple
        return (headerinfo, self.additional_headers or [], body)

    def get_attachments(self, fields, request):
        """Return all attachments uploaded in form.
        """

        attachments = []

        for fname in fields:
            field = fields[fname]
            if INamedFile.providedBy(field) and (getattr(self, 'showAll', True) or fname in getattr(self, 'showFields', ())):
                data = field.data
                filename = field.filename
                mimetype, enc = guess_content_type(filename, data, None)
                attachments.append((filename, mimetype, enc, data))
        return attachments

    def get_mail_text(self, fields, request):
        """Get header and body of e-mail as text (string)
        """

        (headerinfo, additional_headers,
         body) = self.get_header_body_tuple(fields, request)

        if not isinstance(body, unicode):
            body = unicode(body, 'UTF-8')
        email_charset = 'utf-8'
        # always use text/plain for encrypted bodies
        subtype = getattr(
            self, 'gpg_keyid', False) and 'plain' or self.body_type or 'html'
        mime_text = MIMEText(body.encode(email_charset, 'replace'),
                             _subtype=subtype, _charset=email_charset)

        attachments = self.get_attachments(fields, request)

        if attachments:
            outer = MIMEMultipart()
            outer.attach(mime_text)
        else:
            outer = mime_text

        # write header
        for key, value in headerinfo.items():
            outer[key] = value

        # write additional header
        for a in additional_headers:
            key, value = a.split(':', 1)
            outer.add_header(key, value.strip())

        for attachment in attachments:
            filename = attachment[0]
            ctype = attachment[1]
            # encoding = attachment[2]
            content = attachment[3]

            if ctype is None:
                ctype = 'application/octet-stream'

            maintype, subtype = ctype.split('/', 1)

            if maintype == 'text':
                msg = MIMEText(content, _subtype=subtype)
            elif maintype == 'image':
                msg = MIMEImage(content, _subtype=subtype)
            elif maintype == 'audio':
                msg = MIMEAudio(content, _subtype=subtype)
            else:
                msg = MIMEBase(maintype, subtype)
                msg.set_payload(content)
                # Encode the payload using Base64
                Encoders.encode_base64(msg)

            # Set the filename parameter
            msg.add_header(
                'Content-Disposition', 'attachment', filename=filename)
            outer.attach(msg)

        return outer.as_string()

    def onSuccess(self, fields, request):
        """
        e-mails data.
        """
        context = get_context(self)
        mailtext = self.get_mail_text(fields, request)
        host = context.MailHost
        host.send(mailtext)


class CustomScript(Action):
    implements(ICustomScript)
    __doc__ = ICustomScript.__doc__

    def __init__(self, **kw):
        for i, f in ICustomScript.namesAndDescriptions():
            setattr(self, i, kw.pop(i, f.default))
        super(CustomScript, self).__init__(**kw)

    def getScript(self, context):
        # Generate Python script object

        body = self.ScriptBody
        role = self.ProxyRole
        script = PythonScript(self.__name__)
        script = script.__of__(context)

        # Skip check roles
        script._validateProxy = lambda i=None: None

        # Force proxy role
        if role != u'none':
            script.manage_proxy((role,))

        body = body.encode('utf-8')
        params = 'fields, formulator, request'
        script.ZPythonScript_edit(params, body)
        return script

    def sanifyFields(self, form):
        # Makes request.form fields accessible in a script
        #
        # Avoid Unauthorized exceptions since request.form is inaccesible

        result = {}
        for field in form:
            result[field] = form[field]
        return result

    def checkWarningsAndErrors(self, script):
        # Raise exception if there has been bad things with the script
        # compiling

        if len(script.warnings) > 0:
            logger.warn('Python script ' + self.__name__
                        + ' has warning:' + str(script.warnings))

        if len(script.errors) > 0:
            logger.error('Python script ' + self.__name__
                         + ' has errors: ' + str(script.errors))
            raise ValueError(
                'Python script ' + self.__name__ + ' has errors: ' + str(script.errors))

    def executeCustomScript(self, result, form, req):
        # Execute in-place script

        # @param result Extracted fields from request.form
        # @param form Formulator object

        script = self.getScript(form)
        self.checkWarningsAndErrors(script)
        response = script(result, form, req)
        return response

    def onSuccess(self, fields, request):
        # Executes the custom script
        form = get_context(self)
        resultData = self.sanifyFields(request.form)
        return self.executeCustomScript(resultData, form, request)


class SaveData(Action):
    implements(ISaveData)
    __doc__ = ISaveData.__doc__

    def __init__(self, **kw):
        for i, f in ISaveData.namesAndDescriptions():
            setattr(self, i, kw.pop(i, f.default))
        super(SaveData, self).__init__(**kw)

    @property
    def _storage(self):
        context = get_context(self)
        if not hasattr(context, '_inputStorage'):
            context._inputStorage = {}
        if not self.__name__ in context._inputStorage:
            context._inputStorage[self.__name__] = SavedDataBTree()
        return context._inputStorage[self.__name__]

    def clearSavedFormInput(self):
        # convenience method to clear input buffer
        self._storage.clear()

    def getSavedFormInput(self):
        """ returns saved input as an iterable;
            each row is a sequence of fields.
        """

        return self._storage.values()

    def getSavedFormInputItems(self):
        """ returns saved input as an iterable;
            each row is an (id, sequence of fields) tuple
        """
        return self._storage.items()

    def getSavedFormInputForEdit(self, header=False, delimiter=','):
        """ returns saved as CSV text """
        sbuf = StringIO()
        writer = csvwriter(sbuf, delimiter=delimiter)
        names = self.getColumnNames()
        if header:
            writer.writerow(names)
        for row in self.getSavedFormInput():
            writer.writerow([
                row[i].filename if INamedFile.providedBy(
                    row.get(i, '')) else row.get(i, '')
                for i in names])
        res = sbuf.getvalue()
        sbuf.close()
        return res

    def getColumnNames(self):
        # """Returns a list of column names"""
        context = get_context(self)
        showFields = getattr(self, 'showFields', [])
        names = [
            name
            for name, field in getFieldsInOrder(get_fields(context))
            if not showFields or name in showFields
        ]
        if self.ExtraData:
            for f in self.ExtraData:
                names.append(f)
        return names

    def getColumnTitles(self):
        # """Returns a list of column titles"""
        context = get_context(self)
        showFields = getattr(self, 'showFields', [])
        names = [
            field.title
            for name, field in getFieldsInOrder(get_fields(context))
            if not showFields or name in showFields
        ]
        if self.ExtraData:
            for f in self.ExtraData:
                names.append(IExtraData[f].title)
        return names

    def download_csv(self, response):
        # """Download the saved data as csv
        # """
        filename = '%s.csv' % self.__name__
        response.setHeader(
            'Content-Disposition', 'attachment; filename="%s"' % filename)
        response.setHeader('Content-Type', 'text/comma-separated-values')
        response.write(self.getSavedFormInputForEdit(
            getattr(self, 'UseColumnNames', False), delimiter=','))

    def download_tsv(self, response):
        # """Download the saved data as tsv
        # """
        filename = '%s.tsv' % self.__name__
        response.setHeader(
            'Content-Disposition', 'attachment; filename="%s"' % filename)
        response.setHeader('Content-Type', 'text/tab-separated-values')
        response.write(self.getSavedFormInputForEdit(
            getattr(self, 'UseColumnNames', False), delimiter='\t'))

    def download(self, response):
        # """Download the saved data
        # """
        format = getattr(self, 'DownloadFormat', 'tsv')
        if format == 'tsv':
            return self.download_tsv(response)
        else:
            assert format == 'csv', 'Unknown download format'
            return self.download_csv(response)

    def itemsSaved(self):
        return len(self._storage)

    def delDataRow(self, key):
        del self._storage[key]

    def setDataRow(self, key, value):
        #sdata = self.storage[id]
        # sdata.update(data)
        #self.storage[id] = sdata
        self._storage[key] = value

    def addDataRow(self, value):
        storage = self._storage
        if isinstance(storage, IOBTree):
            # 32-bit IOBTree; use a key which is more likely to conflict
            # but which won't overflow the key's bits
            id = storage.maxKey() + 1
        else:
            # 64-bit LOBTree
            id = int(time() * 1000)
            while id in storage:  # avoid collisions during testing
                id += 1
        value['id'] = id
        storage[id] = value

    def onSuccess(self, fields, request):
        """
        saves data.
        """
        # if LP_SAVE_TO_CANONICAL and not loopstop:
            # LinguaPlone functionality:
            # check to see if we're in a translated
            # form folder, but not the canonical version.
            #parent = self.aq_parent
            # if safe_hasattr(parent, 'isTranslation') and \
               # parent.isTranslation() and not parent.isCanonical():
                # look in the canonical version to see if there is
                # a matching (by id) save-data adapter.
                # If so, call its onSuccess method
                #cf = parent.getCanonical()
                #target = cf.get(self.getId())
                # if target is not None and target.meta_type == 'FormSaveDataAdapter':
                    #target.onSuccess(fields, request, loopstop=True)
                    # return
        data = {}
        showFields = getattr(self, 'showFields', []) or self.getColumnNames()
        for f in fields:
            if f not in showFields:
                continue
            data[f] = fields[f]

        if self.ExtraData:
            for f in self.ExtraData:
                if f == 'dt':
                    data[f] = str(DateTime())
                else:
                    data[f] = getattr(request, f, '')

        self.addDataRow(data)


MailerAction = ActionFactory(
    Mailer, _(u'label_mailer_action', default=u'Mailer'))
CustomScriptAction = ActionFactory(
    CustomScript, _(u'label_customscript_action', default=u'Custom Script'))
SaveDataAction = ActionFactory(
    SaveData, _(u'label_savedata_action', default=u'Save Data'))

MailerHandler = BaseHandler(Mailer)
CustomScriptHandler = BaseHandler(CustomScript)
SaveDataHandler = BaseHandler(SaveData)