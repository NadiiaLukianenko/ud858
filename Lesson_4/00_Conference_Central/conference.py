#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'


from datetime import datetime

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote
from google.appengine.ext import ndb
from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import TeeShirtSize
from models import ConferenceForms
from models import ConferenceQueryForms
from utils import getUserId
from settings import WEB_CLIENT_ID
from models import Conference
from models import ConferenceForm
from models import BooleanMessage
from models import ConflictException
from google.appengine.api import memcache
from models import StringMessage
from google.appengine.api import taskqueue
from models import Session
from models import SessionForm
from models import Speaker
from models import SpeakerForm
from models import SessionForms
from models import SpeakerForms

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": [ "Default", "Topic" ]
}

DEFAULTS_SESSION = {
    "typeOfSession": "lecture",
    "highlights": "",
    "duration": "1h"
}

OPERATORS = {
            'EQ':   '=',
            'GT':   '>',
            'GTEQ': '>=',
            'LT':   '<',
            'LTEQ': '<=',
            'NE':   '!='
            }

FIELDS =    {
            'CITY': 'city',
            'TOPIC': 'topics',
            'MONTH': 'month',
            'MAX_ATTENDEES': 'maxAttendees',
            }

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)
SESS_GET_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeConferenceKey=messages.StringField(1),
)
SESS_GET_REQUEST_KEY = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeKey=messages.StringField(1)
)

SESS_GET_REQUEST_SPEAKER = endpoints.ResourceContainer(
    message_types.VoidMessage,
    speakerEmail=messages.StringField(1),
)
SESS_GET_REQUEST_CONF = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)
SESS_GET_REQUEST_TYPE = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
    typeOfSession=messages.StringField(2),
)


MEMCACHE_ANNOUNCEMENTS_KEY = "Test"

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -


@endpoints.api(name='conference',
                version='v1',
                allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID],
                scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # get Profile from datastore
        user_id = getUserId(user)
        p_key = ndb.Key(Profile, user_id)
        profile = p_key.get()
        # create new Profile if not there
        if not profile:
            profile = Profile(
                key = p_key,
                displayName = user.nickname(),
                mainEmail= user.email(),
                teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()

        return profile      # return Profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
            prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)


    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()


    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)


# - - - Conference objects - - - - - - - - - - - - - - - - - - -
    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf


    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        # both for data model & outbound Message
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
            setattr(request, "seatsAvailable", data["maxAttendees"])

        # make Profile Key from user ID
        p_key = ndb.Key(Profile, user_id)
        # allocate new Conference ID with Profile key as parent
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        # make Conference key from ID
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference & return (modified) ConferenceForm
        Conference(**data).put()
        taskqueue.add(params={'email': user.email(),
            'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email'
        )
        return request


    @endpoints.method(ConferenceForm, ConferenceForm,
                  path='conference',
                  http_method='POST',
                  name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)


    @endpoints.method(ConferenceQueryForms, ConferenceForms,
        path='queryConferences', http_method='POST',  name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        #conferences = Conference.query()
        #conferences.fetch()
        conferences = self._getQuery(request)

        # return individual ConferenceForm object per Conference
        return ConferenceForms(
        items=[self._copyConferenceToForm(conf, "")
               for conf in conferences]
        )

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
        path='getConferencesCreated', http_method='POST',
        name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # make profile key
        p_key = ndb.Key(Profile, getUserId(user))
        # create ancestor query for this user
        conferences = Conference.query(ancestor=p_key)
        # get the user profile and display name
        prof = p_key.get()
        displayName = getattr(prof, 'displayName')
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, displayName)
                   for conf in conferences])


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
        path='filterPlayground',
        http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        q = Conference.query()
        # simple filter usage:
        #q = q.filter(Conference.city == "Paris")

        # advanced filter building and usage
        # field = "city"
        # operator = "="
        # value = "London"
        # f = ndb.query.FilterNode(field, operator, value)
        # q = q.filter(f)

        # TODO
        # add 2 filters:
        # 1: city equals to London
        # 2: topic equals "Medical Innovations"
        q = q.filter(Conference.city == "London")
        q = q.filter(Conference.topics == "Medical Innovations")
        q = q.order(Conference.name)
        q = q.filter(Conference.maxAttendees > 10)

        return ConferenceForms(
        items=[self._copyConferenceToForm(conf, "") for conf in q]
        )

# TODO
    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q


    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)

# ------- Registration

    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)

    @ndb.transactional(xg=True)
    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/attending',
            http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        prof = self._getProfileFromUser() # get user Profile
        conf_keys = [ndb.Key(urlsafe=wsck)
                     for wsck in prof.conferenceKeysToAttend]
        conferences = ndb.get_multi(conf_keys)

        # get organizers
        organisers = [ndb.Key(Profile, conf.organizerUserId) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[self._copyConferenceToForm(conf,
        names[conf.organizerUserId]) for conf in conferences]
        )

# - - - Announcements - - - - - - - - - - - - - - - - - - - -


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/announcement/get',
            http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        # TODO 1
        # return an existing announcement from Memcache or an empty string.
        announcement = memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY)
        return StringMessage(data=announcement)

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = '%s %s' % (
                'Last chance to attend! The following conferences '
                'are nearly sold out:',
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)
        return announcement

# ============== Sessions
    def _createSessionObject(self, request):
        """Create or update Session object, returning SessionForm/request."""
        # preload necessary data items

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf_key = ndb.Key(urlsafe=wsck)
        conf = conf_key.get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)
        user = endpoints.get_current_user()
        user_id = getUserId(user)
        # return Conference by key
        if not user_id == conf.organizerUserId:
            raise endpoints.UnauthorizedException("You should be organizer of conference")
        else:
            if not request.sessionName:
                raise endpoints.BadRequestException("Session 'name' field required")
            # copy SessionForm/ProtoRPC Message into dict
            data = {}
            data['sessionName'] = request.sessionName
            data['typeOfSession'] = request.typeOfSession
            data['highlights'] = request.highlights
            data['duration'] = request.duration
            data['date'] = request.date
            data['startTime'] = request.startTime

            # add default values for those missing (both data model & outbound Message)
            for df in DEFAULTS_SESSION:
                if data[df] in (None, []):
                    data[df] = DEFAULTS_SESSION[df]
                    setattr(request, df, DEFAULTS_SESSION[df])

            # convert dates from strings to Date objects; set month based on start_date
            if data['date']:
                sessionDate = datetime.strptime(data['date'][:10], "%Y-%m-%d").date()
                if sessionDate >= conf.startDate and \
                                sessionDate <= conf.endDate:
                    data['date'] = sessionDate
                else:
                    raise endpoints.BadRequestException("Session date is incorrect")

            if data['startTime']:
                data['startTime'] = datetime.strptime(data['startTime'],
                                                      "%H:%M").time()

            data['conferenceKey'] = conf_key
            # make parent Key from conference ID
            p_key = ndb.Key(Conference, wsck)
            # allocate new Conference ID with Profile key as parent
            c_id = Session.allocate_ids(size=1, parent=p_key)[0]
            # make Session key from ID
            c_key = ndb.Key(Session, c_id, parent=p_key)
            data['key'] = c_key

            # Find and add keySpeaker by e-mail
            speakerKey = ndb.Key(Speaker, request.speakerEmail)
            speaker = speakerKey.get()
            if not speaker:
                raise endpoints.BadRequestException("Speaker email is incorrect")

            data['keySpeaker'] = speakerKey

        # create Session & return (modified) SessionForm
        Session(**data).put()
        return BooleanMessage(data=True)

    @endpoints.method(SESS_GET_REQUEST, BooleanMessage,
                  path='conference/{websafeConferenceKey}/session',
                  http_method='POST',
                  name='createSession')
    def createSession(self, request):
        """Create new session."""
        return self._createSessionObject(request)

    def _copySpeakerToForm(self, speaker):
        sf = SpeakerForm()
        for field in sf.all_fields():
            print "Field %s" % field.name
            if hasattr(speaker, field.name):
                setattr(sf, field.name, str(getattr(speaker, field.name)))
            elif field.name == 'websafeKey':
                setattr(sf, field.name, speaker.key.urlsafe())
        sf.check_initialized()
        return sf

    def _addSpeaker(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        c_key = ndb.Key(Speaker, request.speakerEmail)
        sp = c_key.get()
        if not sp:
            sp = Speaker(
                key=c_key,
                speakerName=request.speakerName,
                speakerEmail=request.speakerEmail,
                specialization=request.specialization,
                currentWorkingPlace=request.currentWorkingPlace,
                )
        sp.put()
        return sp      # return Speaker

    @endpoints.method(SpeakerForm, SpeakerForm,
                      path='conference/speaker',
                      http_method='POST',
                      name='addSpeaker')
    def addSpeaker(self, request):
        """Add new Speaker."""
        return self._addSpeaker(request)

    @endpoints.method(SESS_GET_REQUEST_SPEAKER, SpeakerForms,
                      path='getSpeaker',
                      http_method='GET',
                      name='getSpeaker')
    def getSpeaker(self, request):
        """Get speaker"""
        s_key = ndb.Key(Speaker, request.speakerEmail)
        speaker = s_key.get()
        return SpeakerForms(item=self._copySpeakerToForm(speaker))

#!!!------ Sessions query
###############################
    def _copySessionToForm(self, session):
        """Copy relevant fields from Session to SessionForm."""
        sf = SessionForm()
        for field in sf.all_fields():
            if hasattr(session, field.name):
                setattr(sf, field.name, str(getattr(session, field.name)))
            elif field.name == 'websafeKey':
                setattr(sf, field.name, session.key.urlsafe())
            elif field.name == 'speakerName':
                setattr(sf, field.name,
                        getattr(session.keySpeaker.get(), 'speakerName'))
            elif field.name == 'conferenceName':
                setattr(sf, field.name,
                        getattr(session.conferenceKey.get(), 'name'))

        sf.check_initialized()
        return sf


    @endpoints.method(SESS_GET_REQUEST_CONF, SessionForms,
        path='getConferenceSessions', http_method='GET',
        name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """Return sessions in conference."""
        # make conference key
        c_key = ndb.Key(Conference, request.websafeConferenceKey)
        # create ancestor query for this conference
        sessions = Session.query(ancestor=c_key)
        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(s)
                   for s in sessions])


    @endpoints.method(SESS_GET_REQUEST_SPEAKER, SessionForms,
        path='getSessionsBySpeaker', http_method='GET',
        name='getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """Return sessions by speaker."""
        # make key
        s_key = ndb.Key(Speaker, request.speakerEmail)
        # create ancestor query for this conference
        sessions = Session.query(Session.keySpeaker==s_key)
        # return set of ConferenceForm objects per Conference
        return SessionForms(
            items=[self._copySessionToForm(s)
                   for s in sessions])


    @endpoints.method(SESS_GET_REQUEST_TYPE, SessionForms,
                      path='getConferenceSessionsByType',
                      http_method='GET',
                      name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """Return sessions by type for conference"""
        # make key
        c_key = ndb.Key(Conference, request.websafeConferenceKey)
        sessions = Session.query(ancestor=c_key)
        sessions = sessions.filter(Session.typeOfSession==request.typeOfSession)
        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(s)
                   for s in sessions])



    @endpoints.method(SESS_GET_REQUEST_KEY, BooleanMessage,
                      path='session/towishlist',
                      http_method='POST',name='addSessionToWishlist')
    def addSessionToWishlist(self, request):
        """Add session to user's wishlist"""
        return self._sessionToWishlist(request)

    def _sessionToWishlist(self, request, wish=True):
        retval = None
        prof = self._getProfileFromUser()
        #get session, check that it exists
        wsck = request.websafeKey
        sess = ndb.Key(urlsafe=wsck).get()
        if not sess:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % wsck)
        # add to wishlist
        if wish:
            # check if user already registered otherwise add
            if wsck in prof.sessionKeysToWishList:
                raise ConflictException(
                    'You have already added session to wishlist')

            # add session to user's wish list
            prof.sessionKeysToWishList.append(wsck)
            retval = True
        else:
            if wsck in prof.sessionKeysToWishList:
                prof.sessionKeysToWishList.remove(wsck)
                retval=True
            else:
                retval=False

        # write things back to the data store & return
        prof.put()
        return BooleanMessage(data=retval)

    # TODO: test this endpoint
    @endpoints.method(SESS_GET_REQUEST, SessionForms,
            path='sessions/wishlist',
            http_method='GET', name='getConferencesToAttend')
    def getSessionsFromWishList(self, request):
        """Get list of session from conference that user is interested in."""
        prof = self._getProfileFromUser() # get user Profile
        # get all session's keys from user wishlist
        sess_keys_user = [ndb.Key(urlsafe=wsck)
                     for wsck in prof.sessionKeysToWishList]
        # get session's keys from conference
        c_key = ndb.Key(Conference, request.websafeConferenceKey)
        # create ancestor query for this conference
        sessions = Session.query(ancestor=c_key)
        sess_keys_conf = [session.key.urlsafe() for session in sessions]
        sess_keys = set(sess_keys_user) & sess_keys_conf
        sessions = ndb.get_multi(sess_keys)
        return SessionForms(
            items=[self._copySessionToForm(s)
                   for s in sessions])

    @endpoints.method(SESS_GET_REQUEST, BooleanMessage,
                          path='sessions/deletefromwishlist',
                          http_method='GET', name='deleteFromWishlist')
    def deleteFromWishlist(self, request):
        return self._sessionToWishlist(request, wish=False)


# registers API
api = endpoints.api_server([ConferenceApi]) 
