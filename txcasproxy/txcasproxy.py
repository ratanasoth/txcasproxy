#! /usr/bin/env python

#Standard library
import cookielib
import datetime
import os.path
import pprint
import socket
from urllib import urlencode
import urlparse

# Application modules
from ca_trust import MyCATrustRoot

#External modules
from dateutil.parser import parse as parse_date
from klein import Klein

from OpenSSL import crypto

import treq
#from twisted.internet.defer import inlineCallbacks
from twisted.internet import reactor
from twisted.internet.ssl import Certificate
from twisted.python import log
import twisted.web.client as twclient
from twisted.web.client import BrowserLikePolicyForHTTPS, Agent
from twisted.web.client import HTTPConnectionPool
from twisted.web.static import File
from lxml import etree



class ProxyApp(object):
    app = Klein()

    ns = "{http://www.yale.edu/tp/cas}"
    port = None
    
    logout_instant_skew = 5
    
    ticket_name = 'ticket'
    service_name = 'service'
    renew_name = 'renew'
    pgturl_name = 'pgtUrl'
    
    def __init__(self, proxied_url, cas_info, fqdn=None, authorities=None):
        self.proxied_url = proxied_url
        p = urlparse.urlparse(proxied_url)
        self.p = p
        netloc = p.netloc
        self.proxied_host = netloc.split(':')[0]
        self.cas_info = cas_info
        
        cas_param_names = set([])
        cas_param_names.add(self.ticket_name.lower())
        cas_param_names.add(self.service_name.lower())
        cas_param_names.add(self.renew_name.lower())
        cas_param_names.add(self.pgturl_name.lower())
        self.cas_param_names = cas_param_names
        
        if fqdn is None:
            fqdn = socket.getfqdn()
        self.fqdn = fqdn
        
        self.valid_sessions = {}
        self.logout_tickets = {}
        
        self._make_agent(authorities)

    def _make_agent(self, auth_files):
        """
        """
        if auth_files is None or len(auth_files) == 0:
            self.agent = None
        else:
            trustRoot = MyCATrustRoot(extra_cert_paths=auth_files)
            contextFactory = BrowserLikePolicyForHTTPS(trustRoot)
            agent = Agent(reactor, contextFactory=contextFactory)
            self.agent = agent

    def mod_headers(self, h):
        keymap = {}
        for k,v in h.iteritems():
            key = k.lower()
            if key in keymap:
                keymap[key].append(k)
            else:
                keymap[key] = [k]
                
        if 'host' in keymap:
            for k in keymap['host']:
                h[k] = [self.proxied_host]
        if 'content-length' in keymap:
            for k in keymap['content-length']:
                del h[k]
        return h

    def _check_for_logout(self, request):
        """
        """
        data = request.content.read()
        samlp_ns = "{urn:oasis:names:tc:SAML:2.0:protocol}"
        try:
            root = etree.fromstring(data)
        except Exception as ex:
            log.msg("[DEBUG] Not XML.\n%s" % str(ex))
            root = None
        if (root is not None) and (root.tag == "%sLogoutRequest" % samlp_ns):
            instant = root.get('IssueInstant')
            if instant is not None:
                log.msg("[DEBUG] instant string == '%s'" % instant)
                try:
                    instant = parse_date(instant)
                except ValueError:
                    log.msg("[WARN] Odd issue_instant supplied: '%s'." % instant)
                    instant = None
                if instant is not None:
                    utcnow = datetime.datetime.utcnow()
                    log.msg("[DEBUG] UTC now == %s" % utcnow.strftime("%Y-%m-%dT%H:%M:%S"))
                    seconds = abs((utcnow - instant.replace(tzinfo=None)).total_seconds())
                    if seconds <= self.logout_instant_skew:
                        results = root.findall("%sSessionIndex" % samlp_ns)
                        if len(results) == 1:
                            result = results[0]
                            ticket = result.text
                            log.msg("[INFO] Received request to logout session with ticket '%s'." % ticket)
                            sess_uid = self.logout_tickets.get(ticket, None)
                            if sess_uid is not None:
                                self._expired(sess_uid)
                                return True
                            else:
                                log.msg("[WARN] No matching session for logout request for ticket '%s'." % ticket)
                    else:
                        log.msg("[DEBUG] Issue instant was not within %d seconds of actual time." % self.logout_instant_skew)
                else:
                    log.msg("[DEBUG] Could not parse issue instant.")
            else:
                log.msg("[DEBUG] 'IssueInstant' attribute missing from root.")
        elif root is None:
            log.msg("[DEBUG] Could not parse XML.")
        else:
            log.msg("[DEBUG] root.tag == '%s'" % root.tag)
            
        return False

    @app.route("/", branch=True)
    def proxy(self, request):
        """
        """
        valid_sessions = self.valid_sessions
        sess = request.getSession()
        sess_uid = sess.uid
        if not sess_uid in valid_sessions:
            if request.method == 'POST':
                headers = request.requestHeaders
                if headers.hasHeader("Content-Type"):
                    ct_list =  headers.getRawHeaders("Content-Type") 
                    log.msg("[DEBUG] ct_list: %s" % str(ct_list))
                    for ct in ct_list:
                        if ct.find('text/xml') != -1 or ct.find('application/xml') != -1:
                            if self._check_for_logout(request):
                                return ""
                            else:
                                # If reading the body failed the first time, it won't succeed later!
                                log.msg("[DEBUG] _check_for_logout() returned failure.")
                                break
                else:
                    log.msg("[DEBUG] No content-type.")
                            
            # CAS Authentication
            # Does this request have a ticket?  I.e. is it coming back from a successful
            # CAS authentication?
            args = request.args
            ticket_name = self.ticket_name
            if ticket_name in args:
                values = args[ticket_name]
                if len(values) == 1:
                    ticket = values[0]
                    d = self.validate_ticket(ticket, request)
                    return d
            # No ticket (or a problem with the ticket)?
            # Off to CAS you go!
            d = self.redirect_to_cas_login(request)
            return d
        else:
            d = self.reverse_proxy(request)
            return d
        
    #def clean_params(self, qs_map):
    #    """
    #    Remove any CAS-specific parameters from a query string map.
    #    """
    #    cas_param_names = self.cas_param_names
    #    del_keys = []
    #    for k, v in qs_map.iteritems():
    #        if k.lower() in cas_param_names:
    #            del_keys.append(k)
    #    for k in del_keys:
    #        del qs_map[k]
        
    def get_url(self, request):
        """
        """
        fqdn = self.fqdn
        port = self.port
        if port is None:
            port = 443
        if port == 443:
            return urlparse.urljoin("https://%s" % fqdn, request.uri)
        else:
            return urlparse.urljoin("https://%s:%d" % (fqdn, port), request.uri)
        
    def redirect_to_cas_login(self, request):
        """
        """
        cas_info = self.cas_info
        login_url = cas_info['login_url']
                
        p = urlparse.urlparse(login_url)
        params = {self.service_name: self.get_url(request)}
    
        if p.params == '':
            param_str = urlencode(params)
        else:
            param_str = p.params + '&' + urlencode(params)
        p = urlparse.ParseResult(*tuple(p[:4] + (param_str,) + p[5:]))
        url = urlparse.urlunparse(p)
        d = request.redirect(url)
        return d
        
    def validate_ticket(self, ticket, request):
        """
        """
        service_name = self.service_name
        ticket_name = self.ticket_name
        
        this_url = self.get_url(request)
        p = urlparse.urlparse(this_url)
        qs_map = urlparse.parse_qs(p.params)
        if ticket_name in qs_map:
            del qs_map[ticket_name]
        param_str = urlencode(qs_map)
        p = urlparse.ParseResult(*tuple(p[:4] + (param_str,) + p[5:]))
        service_url = urlparse.urlunparse(p)
        
        params = {
                service_name: service_url,
                ticket_name: ticket,}
        param_str = urlencode(params)
        p = urlparse.urlparse(self.cas_info['service_validate_url'])
        p = urlparse.ParseResult(*tuple(p[:4] + (param_str,) + p[5:]))
        service_validate_url = urlparse.urlunparse(p)
        
        log.msg("[INFO] requesting URL '%s' ..." % service_validate_url)
        kwds = {}
        kwds['agent'] = self.agent
        
        d = treq.get(service_validate_url, **kwds)
        d.addCallback(treq.content)
        d.addCallback(self.parse_sv_results, service_url, ticket, request)
        return d
        
    def parse_sv_results(self, payload, service_url, ticket, request):
        """
        """
        log.msg("[INFO] Parsing /serviceValidate results  ...")
        ns = self.ns
        root = etree.fromstring(payload)
        if root.tag != ('%sserviceResponse' % ns):
            return request.redirect(service_url)
        results = root.findall("%sauthenticationSuccess" % ns)
        if len(results) != 1:
            return request.redirect(service_url)
        success = results[0]
        results = success.findall("%suser" % ns)
        if len(results) != 1:
            return request.redirect(service_url)
        user = results[0]
        username = user.text
        
        # Update session session
        valid_sessions = self.valid_sessions
        logout_tickets = self.logout_tickets
        sess = request.getSession()
        sess_uid = sess.uid
        valid_sessions[sess_uid] = {
            'username': username,
            'ticket': ticket,}
        if not ticket in logout_tickets:
            logout_tickets[ticket] = sess_uid
            
        sess.notifyOnExpire(lambda: self._expired(sess_uid))
        
        # Reverse proxy.
        return request.redirect(service_url)
        
    def _expired(self, uid):
        """
        """
        valid_sessions = self.valid_sessions
        if uid in valid_sessions:
            session_info = valid_sessions[uid]
            username = session_info['username']
            ticket = session_info['ticket']
            del valid_sessions[uid]
            logout_tickets = self.logout_tickets
            if ticket in logout_tickets:
                del logout_tickets[ticket]
            log.msg("[INFO] label='Expired session.' session_id='%s' username='%s'" % (uid, username))
        
        
    def reverse_proxy(self, request):
        # Normal reverse proxying.
        kwds = {}
        cookiejar = cookielib.CookieJar()
        kwds['agent'] = self.agent
        kwds['cookies'] = cookiejar
        kwds['headers'] = self.mod_headers(dict(request.requestHeaders.getAllRawHeaders()))
        #print "** HEADERS **"
        #pprint.pprint(self.mod_headers(dict(request.requestHeaders.getAllRawHeaders())))
        #print
        if request.method in ('PUT', 'POST'):
            kwds['data'] = request.content.read()
        #print "request.method", request.method
        #print "url", self.proxied_url + request.uri
        #print "kwds:"
        #pprint.pprint(kwds)
        #print
        d = treq.request(request.method, urlparse.urljoin(self.proxied_url, request.uri), **kwds)
        #print "** Requesting %s %s" % (request.method, self.proxied_url + request.uri)
        def process_headers(response):
            for k,v in response.headers.getAllRawHeaders():
                print "Setting response header: %s: %s" % (k, v)
                request.responseHeaders.setRawHeaders(k, v)
            return response
        d.addCallback(process_headers)
        d.addCallback(treq.content)
        return d
    
