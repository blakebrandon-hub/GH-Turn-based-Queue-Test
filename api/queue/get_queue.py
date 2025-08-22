

@app.route('/api/queue/<int:room_id>')
def get_queue(room_id):
    # Order by id (which reflects insertion order) instead of position
    queue_items = QueueItem.query.filter_by(room_id=room_id)\
                                 .order_by(QueueItem.id)\
                                 .all()
    
    queue_data = []
    for item in queue_items:
        queue_data.append({
            'id': item.id,
            'url': item.video_url,
            'title': item.video_title,
            'duration': item.video_duration,
            'added_by': item.user.username if item.user else 'Unknown',
            'added_at': item.added_at.strftime('%H:%M:%S')
        })
    
    return jsonify({'queue': queue_data})